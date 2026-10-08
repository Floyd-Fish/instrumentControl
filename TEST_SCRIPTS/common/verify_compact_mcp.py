"""compact profile 的**协议级**校验：按真实 MCP stdio 会话起服务并调用工具。

与 verify_compact_profile.py 的分工：
  * verify_compact_profile.py 在**进程内**注册工具、直接调函数——快，但它验证的是
    Python 层，不经过 JSON-RPC，也就证明不了"客户端真能拿到 5 个工具、真能调用成功"。
  * 本脚本起**真实子进程**、走真实 JSON-RPC，验证上线形态。

全程**不碰仪器**：只调 instr_search / instr_describe（纯 registry）与
instr_batch（故意给非法 plan，preflight 直接拒绝，不会提交 executor job），
并用 instr_call 打一个不存在的 op。任何一条真发到设备都会在本脚本里立刻暴露
（设备调用会超时/报 connection 错）。

用法：python TEST_SCRIPTS/common/verify_compact_mcp.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SERVER = ROOT / "mcp_instruments" / "server.py"
PY_EXE = sys.executable

fails: list[str] = []


def _ascii(text) -> str:
    return str(text).encode("ascii", "replace").decode("ascii")


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {_ascii(name):54s} {_ascii(detail)[:110]}", flush=True)
    if not ok:
        fails.append(name)


class Probe:
    """最小 stdio JSON-RPC 客户端（与 dump_mcp_tools.py 同一套手法）。"""

    def __init__(self, profile: str | None, timeout: float = 180.0) -> None:
        env = dict(os.environ)
        if profile is None:
            # profile=None 用来测**代码默认档**：必须把环境变量摘掉，而不是写成 "compact"
            # —— 否则测的是环境变量，不是 server.py 的 _DEFAULT_PROFILE（本机 shell 里
            # 可能恰好也设了它，那样这个检查就永远为真、失去意义）。
            env.pop("INSTRUMENT_MCP_PROFILE", None)
        else:
            env["INSTRUMENT_MCP_PROFILE"] = profile
        self.timeout = timeout
        self.proc = subprocess.Popen([PY_EXE, str(SERVER)], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     bufsize=0, cwd=str(ROOT), env=env)

    def send(self, obj: dict) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def read_line(self, timeout: float | None = None) -> str:
        assert self.proc.stdout is not None
        box: dict = {}
        th = threading.Thread(
            target=lambda: box.update(l=self.proc.stdout.readline().decode("utf-8", "replace")),
            daemon=True)
        th.start()
        th.join(timeout if timeout is not None else self.timeout)
        return box.get("l", "")

    def handshake(self) -> dict:
        self.send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                   "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                              "clientInfo": {"name": "compact-mcp-check", "version": "1"}}})
        line = self.read_line()
        if not line.strip():
            raise RuntimeError("initialize timed out")
        out = json.loads(line)
        if "result" not in out:
            raise RuntimeError(f"initialize failed: {line[:200]}")
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        return out["result"]

    def tools(self) -> list[dict]:
        self.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        line = self.read_line()
        return json.loads(line)["result"]["tools"]

    def call(self, name: str, args: dict, timeout: float | None = None) -> dict:
        """调用一个工具，返回解析后的返回体（工具本身回的是 JSON 字符串）。"""
        self.send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                   "params": {"name": name, "arguments": args}})
        line = self.read_line(timeout)
        if not line.strip():
            return {"_transport": "timeout"}
        obj = json.loads(line)
        if "error" in obj:
            return {"_rpc_error": obj["error"]}
        content = (obj.get("result") or {}).get("content") or []
        text = content[0].get("text") if content else ""
        try:
            return json.loads(text)
        except Exception:                                        # noqa: BLE001
            return {"_raw": text}

    def close(self) -> str:
        try:
            if self.proc.stdin is not None:
                self.proc.stdin.close()
            self.proc.wait(timeout=15)
        except Exception:                                        # noqa: BLE001
            self.proc.kill()
        assert self.proc.stderr is not None
        return self.proc.stderr.read().decode("utf-8", "replace")


def main() -> int:
    print("== compact profile over real MCP stdio ==")

    print("\nS0 the code default is compact (INSTRUMENT_MCP_PROFILE removed from the env)")
    d = Probe(None)
    dtools: dict = {}
    dlog = ""
    try:
        d.handshake()
        dtools = {t["name"]: t for t in d.tools()}
    finally:
        dlog = d.close()
    check("omitting INSTRUMENT_MCP_PROFILE yields the compact table",
          set(dtools) == {"instr_devices", "instr_search", "instr_describe",
                          "instr_call", "instr_batch"}, str(sorted(dtools)))
    dreg = [line for line in dlog.splitlines() if "runtime registry" in line]
    check("default startup self-check reports profile=compact",
          bool(dreg) and "profile=compact" in dreg[-1],
          dreg[-1][:100] if dreg else "(none)")

    p = Probe("compact")
    try:
        info = p.handshake()
        tools = {t["name"]: t for t in p.tools()}

        print("\nS1 surface over the wire")
        check("server reports the compact tool table (5 tools)", len(tools) == 5, str(sorted(tools)))
        check("tool names are the 5 expected ones",
              set(tools) == {"instr_devices", "instr_search", "instr_describe",
                             "instr_call", "instr_batch"}, str(sorted(tools)))
        check("no legacy device tool leaked into compact",
              not any(n.startswith(("dg_", "sds_", "sdg_", "dmm_", "dho_", "mho_", "psu_",
                                    "ks3458a_")) for n in tools),
              str([n for n in tools if n.startswith(("dg_", "sds_", "sdg_"))]))
        check("every compact tool has a non-empty description",
              all((t.get("description") or "").strip() for t in tools.values()), "ok")

        print("\nS2 pure-registry tools over the wire (no device contact)")
        # instr_devices 快速路径：只读配置+缓存，**不扫描**。全量发现实测 355.9s 且会占住
        # 执行器 BUSY——所以默认必须是瞬时的那条路。
        t0 = time.perf_counter()
        r = p.call("instr_devices", {})
        dt = time.perf_counter() - t0
        check("instr_devices defaults to the known-address path",
              r.get("mode") == "known" and r.get("scanned") is False, str(r.get("mode")))
        check("known-address path is instant (no network scan)", dt < 3.0, f"{dt:.2f}s")
        check("known-address result names its source",
              bool(r.get("source")), str(r.get("source"))[:60])
        check("known-address result lists the cached/configured devices",
              isinstance(r.get("devices"), list), f"count={r.get('count')}")

        r = p.call("instr_search", {"query": "measure vpp"})
        check("instr_search returns hits", r.get("ok") and r.get("count", 0) > 0,
              f"count={r.get('count')}")
        r = p.call("instr_search", {"query": "sds", "device": "sds"})
        check("instr_search honours the device filter",
              r.get("ok") and all(x["device"] == "sds" for x in r.get("results", [])),
              f"count={r.get('count')}")
        r = p.call("instr_describe", {"ids": "sds_measure"})
        op = (r.get("operations") or [{}])[0]
        check("instr_describe returns the parameter table",
              r.get("ok") and "properties" in (op.get("parameters") or {}),
              str(list((op.get("parameters") or {}).get("properties", {}))[:4]))
        check("instr_describe carries the safety classification",
              (op.get("safety") or {}).get("risk") == "read_only",
              str(op.get("safety")))

        print("\nS3 rejection paths over the wire (still no device contact)")
        r = p.call("instr_call", {"op": "no.such.op", "args": {}})
        check("instr_call rejects an unknown op",
              r.get("error_type") == "param_validation", str(r)[:90])
        bad_plan = {"plan_version": 1, "on_error": "stop", "max_leaf_steps": 10,
                    "max_nesting_depth": 1,
                    "steps": [{"op": "no.such.op", "args": {}}]}
        r = p.call("instr_batch", {"plan": bad_plan})
        check("instr_batch rejects an unknown op at preflight",
              r.get("error_type") == "plan_validation", str(r)[:90])
        check("preflight rejection says no instrument operation ran",
              "未执行任何仪器操作" in (r.get("error") or ""), str(r.get("error"))[:80])
        # 作用域越界的 plan：同样必须零仪器调用。
        # ⚠ 这里**故意只用非法引用**，不用真实 op —— 首版曾用 sds.measure 想验证
        # "preflight 放行"这条路径，结果它真的提交了 executor job 并触发网络自动发现
        # （未发 SCPI、未改设备状态，但已属"碰仪器"，与本脚本"零设备接触"的声明不符）。
        # "preflight 放行 → 提交 job"那条路径改由 verify_batch_offline.py 用假 registry 覆盖。
        scoped = {"plan_version": 1, "on_error": "stop", "max_leaf_steps": 10,
                  "max_nesting_depth": 1,
                  "steps": [{"foreach": {"var": "f", "values": [1]},
                             "steps": [{"op": "instr.query", "args": {"resource": "FAKE",
                                                                      "cmd": {"$var": "f"}}}]},
                            {"op": "instr.query", "args": {"resource": "FAKE", "cmd": {"$var": "f"}}}]}
        r = p.call("instr_batch", {"plan": scoped})
        codes = {i.get("code") for i in (r.get("issues") or [])}
        check("instr_batch statically rejects a foreach-capture/var escape",
              r.get("error_type") == "plan_validation" and "undefined_variable" in codes,
              str(codes))
        r = p.call("instr_batch", {"plan": {"plan_version": 1, "on_error": "stop",
                                            "max_leaf_steps": 10, "max_nesting_depth": 0,
                                            "steps": [{"foreach": {"var": "f", "values": [1]},
                                                       "steps": [{"op": "instr.query",
                                                                  "args": {"resource": "FAKE",
                                                                           "cmd": "?"}}]}]}})
        codes = {i.get("code") for i in (r.get("issues") or [])}
        check("instr_batch rejects exceeding max_nesting_depth",
              r.get("error_type") == "plan_validation" and "too_deep" in codes, str(codes))

        print("\nS4 device path through compact, against an unreachable loopback resource")
        # 关键：用**故意不可达**的回环资源（127.0.0.1 的关闭端口）走一遍完整链路——
        # compact → executor job → _invoke_operation → 操作实现 → _call → VISA connect
        # → 错误分类 → 返回体。它会在"连不上"处停下，**因此不接触任何真实仪器**，
        # 却足以证明设备路径真的被打通了（而不是只验证了调度层）。
        # 选 instr.query 是因为它同时经过 policy（raw_scpi 通道的纯查询判据）。
        DEAD = "TCPIP0::127.0.0.1::9::SOCKET"
        r = p.call("instr_call", {"op": "instr.query",
                                  "args": {"resource": DEAD, "cmd": "*IDN?",
                                           "timeout_ms": 500}}, timeout=120)
        check("dead-resource call fails (not silently ok)", r.get("ok") is False, str(r)[:90])
        check("failure is classified as a connection problem (VISA layer was reached)",
              r.get("error_type") == "connection", str(r.get("error_type")))
        check("response echoes the probed resource (no device was actually touched)",
              r.get("resource") == DEAD, str(r.get("resource"))[:60])

        # 同一个死资源走 batch：验证批量路径也真的驱动了操作实现
        dead_plan = {"plan_version": 1, "on_error": "continue", "max_leaf_steps": 8,
                     "max_nesting_depth": 1,
                     "steps": [{"foreach": {"var": "i", "values": [1, 2, 3]},
                                "steps": [{"op": "instr.query",
                                           "args": {"resource": DEAD, "cmd": "*IDN?",
                                                    "timeout_ms": 500}}]}]}
        b = p.call("instr_batch", {"plan": dead_plan}, timeout=300)
        check("batch drove all 3 leaves through the device path",
              b.get("leaf_count") == 3, str(b.get("leaf_count")))
        check("each leaf failed at the connection layer",
              all((x.get("error_type") == "connection")
                  for x in (b.get("leaf_results") or [])),
              str([x.get("error_type") for x in (b.get("leaf_results") or [])]))
        check("on_error=continue kept every leaf result",
              b.get("failure_count") == 3 and b.get("success_count") == 0,
              f"fail={b.get('failure_count')} ok={b.get('success_count')}")
        check("batch summary was persisted", bool(b.get("result_artifact")),
              str(b.get("result_artifact"))[:70])

        print("\nS5 startup self-check")
        log = p.close()
        reg = [l for l in log.splitlines() if "runtime registry" in l]
        check("startup self-check reports profile=compact and 68 operations",
              bool(reg) and "profile=compact" in reg[-1] and "operations=68" in reg[-1],
              reg[-1][:100] if reg else "(none)")
        check("server advertised its own name/version",
              bool(info.get("serverInfo", {}).get("name")), str(info.get("serverInfo")))
    finally:
        try:
            p.close()
        except Exception:                                        # noqa: BLE001
            pass

    print(f"\n== result: {'all PASS' if not fails else f'{len(fails)} FAIL'} ==")
    for f in fails:
        print(f"  - {_ascii(f)}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
