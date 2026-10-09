"""验证 model/resource 字段语义修复：通用工具 + 设备专用工具回归。"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "dh1766_control" / "src"))
sys.path.insert(0, str(ROOT / "mcp_instruments"))

import server  # noqa: E402

from common.resolver import resolve  # noqa: E402

results = []


def check(name, raw, expect_model=None, expect_resource=None, expect_ok=None):
    r = json.loads(raw)
    ok = True
    msgs = []
    if expect_ok is not None and r.get("ok") != expect_ok:
        ok = False
        msgs.append(f"ok={r.get('ok')} 期望{expect_ok}")
    if expect_model is not None and r.get("model") != expect_model:
        ok = False
        msgs.append(f"model={r.get('model')!r} 期望{expect_model!r}")
    if expect_resource is not None and r.get("resource") != expect_resource:
        ok = False
        msgs.append(f"resource={r.get('resource')!r} 期望{expect_resource!r}")
    results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {json.dumps(r, ensure_ascii=False)[:150]}", flush=True)


print("=== 通用工具：错误结构应有独立 model/resource ===", flush=True)
# 1) 不存在的资源（傅师傅报的场景）
check("instr_query(dummy)", server.instr_query("dummy", "*IDN?"),
      expect_model="instruments", expect_resource="dummy", expect_ok=False)
# 2) cmd 无 ?
check("instr_query 无?", server.instr_query("dummy", "VOLT"),
      expect_model="instruments", expect_resource="dummy", expect_ok=False)
# 3) instr_write 无 confirm
check("instr_write 无confirm", server.instr_write("dummy", "VOLT 1"),
      expect_model="instruments", expect_resource="dummy", expect_ok=False)
# 4) instr_write 黑名单
check("instr_write *RST", server.instr_write("dummy", "*RST", confirm=True),
      expect_model="instruments", expect_resource="dummy", expect_ok=False)
# 5) 真实资源成功路径（model 应为 instruments，resource 为资源串）
# 地址由 common.resolver 解析（不写死 IP，换网段/换口自适应）
r = json.loads(server.instr_query(resolve("sds"), "*IDN?"))
ok = r.get("ok") and r.get("model") == "instruments" and r.get("resource", "").startswith("TCPIP")
results.append(ok)
print(f"[{'PASS' if ok else 'FAIL'}] instr_query 成功路径: {json.dumps(r, ensure_ascii=False)[:150]}", flush=True)

print("=== 设备专用工具回归：model 应为设备名、无 resource 字段 ===", flush=True)
check("sds_status", server.sds_status(), expect_model="SDS", expect_ok=True)
check("psu_status", server.psu_status(), expect_model="DH1766", expect_ok=True)
r = json.loads(server.psu_status())
results.append("resource" not in r)
print(f"[{'PASS' if 'resource' not in r else 'FAIL'}] psu_status 无 resource 字段", flush=True)

print(f"\n结果: {sum(results)}/{len(results)} PASS", flush=True)
