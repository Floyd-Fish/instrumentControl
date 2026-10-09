"""SDS 测量扩展 MCP 工具实测（查询 + 写往返恢复）。"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "dh1766_control" / "src"))
sys.path.insert(0, str(ROOT / "mcp_instruments"))

import server  # noqa: E402

results = []


def show(name, raw, want_ok=True):
    r = json.loads(raw)
    ok = (r.get("ok") is True) == want_ok
    results.append(ok)
    val = r.get("result") if r.get("ok") else r.get("error")
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {json.dumps(val, ensure_ascii=False, default=str)[:170]}", flush=True)


print("=== 查询（只读）===", flush=True)
show("sds_meas_threshold 查询", server.sds_meas_threshold())
show("sds_meas_gate 查询", server.sds_meas_gate())
show("sds_meas_statistics 查询", server.sds_meas_statistics())
show("sds_meas_dtime 查询", server.sds_meas_dtime(1))
show("sds_meas_display 查询", server.sds_meas_display())

print("=== 写往返（改后恢复）===", flush=True)
# 阈值类型往返
orig_type = json.loads(server.sds_meas_threshold())["result"]["type"]
new_type = "ABSolute" if str(orig_type).upper().startswith("PERC") else "PERCent"
show(f"threshold 写 {new_type}", server.sds_meas_threshold(thr_type=new_type))
show("threshold 恢复", server.sds_meas_threshold(thr_type=orig_type))

# 门限开关往返
orig_gate = json.loads(server.sds_meas_gate())["result"]["gate_on"]
show("gate 写取反", server.sds_meas_gate(on=not orig_gate))
show("gate 恢复", server.sds_meas_gate(on=orig_gate))

# 结果显示样式往返
orig_rd = json.loads(server.sds_meas_display())["result"]["rdisplay"]
new_rd = "FLOating" if str(orig_rd).upper().startswith("EMB") else "EMBedded"
show(f"display 写 {new_rd}", server.sds_meas_display(rdisplay=new_rd))
show("display 恢复", server.sds_meas_display(rdisplay=orig_rd))

print(f"\n结果: {sum(results)}/{len(results)} PASS", flush=True)
