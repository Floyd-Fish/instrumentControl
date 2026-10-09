"""MCP 新工具实测（断言版）：sds_measure_phase / psu_mode / psu_set_mode。

注意：当前 C1 无信号（PKPK≈15mV），故跨通道 PHA 预期失败（正确行为）；
工具功能用 PHA(C2,C2)=0 自检验证。
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "dh1766_control" / "src"))
sys.path.insert(0, str(ROOT / "mcp_instruments"))

import server  # noqa: E402

results = []


def expect(name, raw, want_ok=True):
    r = json.loads(raw)
    ok = (r.get("ok") is True) == want_ok
    results.append(ok)
    detail = json.dumps(r.get("result") or r.get("error"), ensure_ascii=False)[:150]
    print(f"[{'PASS' if ok else 'FAIL'}] {name} (期望{'成功' if want_ok else '拒绝'}): {detail}", flush=True)


print("=== SDS 测量 ===", flush=True)
expect("sds_measure(PKPK,C2)", server.sds_measure("PKPK", 2), True)
expect("sds_measure(FREQ,C2)", server.sds_measure("FREQ", 2), True)
expect("sds_measure 非法item", server.sds_measure("BOGUS", 2), False)
expect("sds_measure_phase(C2,C2) 自检≈0", server.sds_measure_phase("C2", "C2"), True)
expect("sds_measure_phase(C2,C1) C1无信号→拒绝", server.sds_measure_phase("C2", "C1"), False)

print("=== PSU 模式 ===", flush=True)
expect("psu_mode", server.psu_mode(), True)
expect("psu_status 含 output_mode", server.psu_status(), True)
expect("psu_set_mode 带载→拒绝", server.psu_set_mode("SERI"), False)
expect("psu_set_mode 非法值→拒绝", server.psu_set_mode("BOGUS"), False)

print(f"\n结果: {sum(results)}/{len(results)} PASS", flush=True)
