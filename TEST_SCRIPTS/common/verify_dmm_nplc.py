"""dmm_nplc 工具实测（34465A 上线验证）：查询 + 写往返恢复。

手册依据：[SENSe:]VOLTage[:DC]:NPLC，取值 0.02/0.2/1/10/100（默认 10）。
注意 NPLC 与 APERture 互斥——本测试只动 NPLC，不动孔径。
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


def show(name, raw, want_ok=True):
    r = json.loads(raw)
    ok = (r.get("ok") is True) == want_ok
    results.append(ok)
    val = r.get("result") if r.get("ok") else r.get("error")
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {json.dumps(val, ensure_ascii=False, default=str)[:150]}", flush=True)


print("=== DMM 状态 ===", flush=True)
show("dmm_status", server.dmm_status())

print("=== NPLC 查询 ===", flush=True)
r = json.loads(server.dmm_nplc())
show("dmm_nplc 查询", server.dmm_nplc())
orig = r["result"]["nplc"] if r["ok"] else None

print("=== NPLC 写往返 ===", flush=True)
for v in (1, 0.2, 10):
    if v == orig:
        continue
    show(f"写 NPLC={v}", server.dmm_nplc(v))
if orig is not None:
    show(f"恢复 NPLC={orig}", server.dmm_nplc(orig))

print("=== 其他 DMM 工具回归 ===", flush=True)
show("dmm_measure volt_dc", server.dmm_measure("volt_dc"))
show("dmm_configure volt_dc", server.dmm_configure("volt_dc", 0.1))

print(f"\n结果: {sum(results)}/{len(results)} PASS", flush=True)
