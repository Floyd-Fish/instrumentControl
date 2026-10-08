"""3458A MCP 工具**注册与元信息**验收（离线；只做 JSON-RPC 握手，**绝不调用设备工具**）。

    python TEST_SCRIPTS/ks3458a/verify_mcp_ks3458a_tools.py

为什么单列一条：`ks3458a_*` 是新增工具族，注册方式走 server.py 的 `@device_tool()`
表驱动（不是逐个 `@mcp.tool`），漏注册/写错参数名/漏 docstring 都只在**协议层**看得见
（客户端里表现为"工具不存在"或"无描述"）。同口径的全量检查在
`TEST_SCRIPTS/common/verify_mcp_tools_meta.py`（那条查所有工具），本条只钉 3458A 这一族。

断言：
    §1 握手正常（initialize / tools/list）
    §2 8 个 `ks3458a_*` 工具**全部在列**，且没有计划外的 `ks3458a_` 工具
    §3 每个工具的 description 非空（FastMCP 取自函数 docstring）
    §4 入参 schema 与设计逐字一致（参数名集合 + 有无必填项）
    §5 stdout 是干净协议流（历史事故：库里的 print 打进协议通道 → 客户端断连）
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SERVER = ROOT / "mcp_instruments" / "server.py"
PY_EXE = sys.executable

# 断言对象是 `ks3458a_*` 这套 legacy 具名工具。server.py 的默认档已改为 compact，
# 不钉住就会一个都找不到。
SERVER_ENV = {**os.environ, "INSTRUMENT_MCP_PROFILE": "legacy"}


# 工具名 → 期望的入参名集合（本文件是"设计"的可执行副本：改了工具必须同步改这里）
EXPECTED_TOOLS: dict[str, set[str]] = {
    "ks3458a_status": {"resource"},
    "ks3458a_read": {"resource"},
    "ks3458a_read_avg": {"n", "resource"},
    "ks3458a_read_stats": {"n", "resource"},
    "ks3458a_read_series": {"n", "interval_s", "save_csv", "resource"},
    "ks3458a_burst": {"n", "sample_interval_s", "dcv_range", "data_format", "save_csv",
                      "resource"},
    "ks3458a_configure": {"dcv_range", "nplc", "resource"},
    "ks3458a_autorange": {"on", "resource"},
    "ks3458a_acv": {"range", "band_lo", "band_hi", "sync", "nplc", "resource"},
    "ks3458a_reset": {"confirm", "resource"},
    "ks3458a_unstick": {"confirm", "resource"},
}

fails: list[str] = []
checks: list[dict] = []


def _ascii(text) -> str:
    """Force ASCII output so the console code page never matters."""
    return str(text).encode("ascii", "replace").decode("ascii")


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {_ascii(name):58s} {_ascii(detail)[:100]}",
          flush=True)
    checks.append({"name": name, "ok": bool(ok), "detail": str(detail)})
    if not ok:
        fails.append(name)


def main() -> int:
        # stdout is the JSON-RPC stream (UTF-8); stderr is the server log. Decode stderr
    # as UTF-8 with errors="replace" and assert on ASCII anchors only, so the check
    # never depends on the machine's console code page.
    proc = subprocess.Popen([PY_EXE, str(SERVER)], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", bufsize=1, cwd=str(ROOT),
                            env=SERVER_ENV)

    def send(obj: dict) -> None:
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()

    def read_line(timeout: float = 180.0) -> str:
        box: dict = {}
        thread = threading.Thread(target=lambda: box.update(line=proc.stdout.readline()),
                                  daemon=True)
        thread.start()
        thread.join(timeout)
        return box.get("line", "")

    print("S1 handshake (initialize / tools/list only; no device tool is called)", flush=True)
    send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
          "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                     "clientInfo": {"name": "ks3458a-tools-check", "version": "1"}}})
    line = read_line()
    check("initialize returns a result", bool(line.strip()) and "result" in json.loads(line),
          line[:70])
    send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
    send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    raw = read_line()
    tools = json.loads(raw)["result"]["tools"] if raw.strip() else []
    by_name = {t["name"]: t for t in tools}
    names = list(by_name)
    check("tools/list returns a tool table", bool(tools), f"{len(tools)} tools")

    print("\nS2 ks3458a_* tool family present", flush=True)
    missing = [n for n in EXPECTED_TOOLS if n not in by_name]
    extra = [n for n in names if n.startswith("ks3458a_") and n not in EXPECTED_TOOLS]
    check("all 11 ks3458a_* tools registered", not missing,
          f"缺 {missing}" if missing else f"{len(EXPECTED_TOOLS)}/{len(EXPECTED_TOOLS)} 全在")
    check("no unexpected ks3458a_ tools", not extra, extra or "none")
    # 只读/写分类是文档口径，这里顺带确认命名前缀一致（便于客户端按前缀过滤）
    check("names share the ks3458a_ prefix",
          all(n.startswith("ks3458a_") for n in EXPECTED_TOOLS))

    print("\nS3 non-empty descriptions", flush=True)
    no_desc = [n for n in EXPECTED_TOOLS if not (by_name.get(n, {}).get("description") or "").strip()]
    check("every ks3458a_* tool has a description", not no_desc, no_desc or "8/8 ok")

    print("\nS4 input schema (parameter names + required)", flush=True)
    bad_params = {}
    required_bad = {}
    for name, expected in EXPECTED_TOOLS.items():
        schema = (by_name.get(name) or {}).get("inputSchema") or {}
        actual = set((schema.get("properties") or {}).keys())
        if actual != expected:
            bad_params[name] = sorted(actual ^ expected)
        req = set(schema.get("required") or [])
        if req:                     # 全部入参都有默认值 → required 必须为空
            required_bad[name] = sorted(req)
    check("parameter names match the design", not bad_params,
          bad_params or f"核对 {len(EXPECTED_TOOLS)} 个工具")
    check("no unexpected required parameters", not required_bad,
          required_bad or "无")

    print("\nS5 stdout purity (JSON-RPC channel)", flush=True)
    extra_lines: list[str] = []

    def drain() -> None:
        while True:
            text = proc.stdout.readline()
            if not text:
                return
            extra_lines.append(text.rstrip("\n"))

    thread = threading.Thread(target=drain, daemon=True)
    thread.start()
    thread.join(2.0)

    def is_jsonrpc(text: str) -> bool:
        try:
            obj = json.loads(text)
        except ValueError:
            return False
        return isinstance(obj, dict) and ("jsonrpc" in obj or "result" in obj)

    polluted = [t for t in extra_lines if t.strip() and not is_jsonrpc(t)]
    check("stdout carries JSON-RPC only (no print pollution)", not polluted, polluted[:2] or "clean")

    print("\nS6 in-tool gates (confirm / param validation; no device I/O here)", flush=True)
    probe_code = (
        "import sys, json; sys.path.insert(0, '.');"
        "import mcp_instruments.server as s;"
        "print('PROBE' + json.dumps({"
        "'reset': s.ks3458a_reset(confirm=False),"
        "'avg0': s.ks3458a_read_avg(n=0),"
        "'stats_big': s.ks3458a_read_stats(n=10**9)}))")
    probe = subprocess.run([PY_EXE, "-c", probe_code], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", cwd=str(ROOT), timeout=300)
    payload: dict = {}
    for text in probe.stdout.splitlines():
        if text.startswith("PROBE"):
            payload = json.loads(text[len("PROBE"):])

    def _inner(key: str) -> dict:
        try:
            return json.loads(payload.get(key) or "")
        except (ValueError, TypeError):
            return {}

    reset_resp = _inner("reset")
    check("ks3458a_reset without confirm -> confirm_required",
          reset_resp.get("error_type") == "confirm_required",
          reset_resp.get("error", "")[:70])
    check("ks3458a_read_avg n=0 -> param_validation",
          _inner("avg0").get("error_type") == "param_validation")
    check("ks3458a_read_stats n out of range -> param_validation",
          _inner("stats_big").get("error_type") == "param_validation")

    proc.stdin.close()
    try:
        proc.wait(timeout=15)
    except Exception:                                # noqa: BLE001
        proc.kill()
    # Server log may be non-ASCII; decode with errors="replace" and assert on ASCII
    # anchors only ([instrumentControl], description, digits).
    err_bytes = proc.stderr.buffer.read()
    err = err_bytes.decode("utf-8", "replace")
    boot = [t for t in err.splitlines() if "[instrumentControl]" in t and "description" in t]
    check("startup self-check line present", bool(boot), boot[-1][:96] if boot else "(none)")
    check("self-check reports 0 missing descriptions",
          bool(boot) and re.search(r"description\s+0\b", boot[-1]) is not None,
          boot[-1][:96] if boot else "(none)")

    total = len(checks)
    passed = total - len(fails)
    print(f"\n== result: {passed}/{total} PASS"
          f"{'' if not fails else f' ({len(fails)} FAIL)'} ==", flush=True)
    for name in fails:
        print(f"  - FAIL: {name}")

    try:
        out_dir = ROOT / "TEST_DATA" / "ks3458a"
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = out_dir / f"verify_mcp_ks3458a_tools_{stamp}.json"
        out_path.write_text(json.dumps({
            "when": datetime.now().isoformat(timespec="seconds"),
            "script": "TEST_SCRIPTS/ks3458a/verify_mcp_ks3458a_tools.py",
            "device_io": "none (handshake only; no device tool was called)",
            "tool_count_total": len(tools),
            "ks3458a_tools": sorted(EXPECTED_TOOLS),
            "passed": passed, "total": total, "fails": fails, "checks": checks,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"evidence: {out_path}", flush=True)
    except Exception as exc:                          # noqa: BLE001
        print(f"evidence write failed (result unaffected): {type(exc).__name__}: {exc}",
              flush=True)

    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
