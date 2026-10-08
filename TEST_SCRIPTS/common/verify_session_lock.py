"""跨进程**会话咨询锁**的离线回归（`common/session_lock.py`；不碰任何仪器）。

背景与口径（2026-09-17，`docs/visa_concurrency_20260917.md`）：
同一台设备两个会话并发会**静默串台**；而本机实测曾同时存在 14 个 instrument MCP 实例。
锁的**判重键 = VISA 地址**（用户指定），同一台设备的**另一接口**只额外告警（跨接口并发未验证）。

    python TEST_SCRIPTS/common/verify_session_lock.py

断言：
    §1 键解析：地址键（大小写/空白规范化）与物理设备键（LAN 取 host、USB 取 vid:pid:sn、ASRL 取口）
    §2 本进程自己刷新 → 自己**不算**占用者
    §3 **另一活跃进程同地址** → 出"同一地址"告警（这是会串台的场景）
    §4 **另一活跃进程同设备另一接口**（inst0 vs 5555）→ 出"跨接口未验证"告警，且**不计入 holders**
    §5 过期（TTL 外）/ 死进程的锁文件 → 被清理且不告警
    §6 目录不可写等异常 → **降级为空告警 + error 字段**，绝不抛异常（锁不阻断工具）
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from common import session_lock as sl  # noqa: E402

fails: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name:62s} {str(detail)[:100]}", flush=True)
    if not ok:
        fails.append(name)


print("§1 键解析", flush=True)
check("地址键：规范化大小写/空白",
      sl.resource_key("  tcpip0::192.168.1.55::inst0::instr ") == "TCPIP0::192.168.1.55::INST0::INSTR")
check("设备键：LAN 取 host（协议/端口不同 → 同一台设备）",
      sl.device_key("TCPIP0::192.168.1.55::inst0::INSTR") == sl.device_key("TCPIP0::192.168.1.55::5555::SOCKET") == "TCPIP::192.168.1.55",
      sl.device_key("TCPIP0::192.168.1.55::inst0::INSTR"))
check("设备键：USB 取 vid:pid:sn",
      sl.device_key("USB0::0x1AB1::0x0643::DG8A265103205::INSTR") == "USB::1AB1:0643:DG8A265103205",
      sl.device_key("USB0::0x1AB1::0x0643::DG8A265103205::INSTR"))
check("设备键：串口取口名", sl.device_key("ASRL5::INSTR") == "ASRL::ASRL5", sl.device_key("ASRL5::INSTR"))
check("不同设备的设备键不同",
      sl.device_key("TCPIP0::192.168.1.55::inst0::INSTR") != sl.device_key("TCPIP0::192.168.1.56::inst0::INSTR"))

# 把锁目录指到临时目录（不污染真实配置）
tmp = Path(tempfile.mkdtemp(prefix="sesslock_"))
sl._locks_dir = lambda: tmp                                   # type: ignore[assignment]
RES = "TCPIP0::192.168.1.55::inst0::INSTR"
RES_RAW = "TCPIP0::192.168.1.55::5555::SOCKET"

print("\n§2 自己刷新不算占用", flush=True)
info = sl.touch(RES, kind="MHO")
check("本进程刷新后 holders 为空、无告警", not info["holders"] and not info["warnings"], str(info["warnings"]))
check("锁文件已落盘（文件名带本进程 PID）",
      (tmp / sl._fname(sl.resource_key(RES), os.getpid())).exists(),
      sl._fname(sl.resource_key(RES), os.getpid()))

print("\n§3 另一**活跃**进程同地址 → 同一地址告警", flush=True)
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
time.sleep(0.4)
rec = {"pid": child.pid, "ts": time.time(), "started": time.time() - 42,
       "resource": RES, "kind": "MHO", "host": "other-box"}
(tmp / "other_live.json").write_text(json.dumps(rec), encoding="utf-8")
info = sl.touch(RES, kind="MHO")
check("检测到同地址占用者", len(info["holders"]) == 1 and info["holders"][0]["pid"] == child.pid,
      f"holders={len(info['holders'])}")
check("告警文案点明'响应串台'与 PID",
      any("同一地址" in w and "串台" in w and str(child.pid) in w for w in info["warnings"]),
      (info["warnings"] or [""])[0][:80])

print("\n§4 另一进程用**同设备另一接口** → 跨接口告警（不计入 holders）", flush=True)
(tmp / "other_live.json").unlink()
(tmp / "other_iface.json").write_text(json.dumps(
    {"pid": child.pid, "ts": time.time(), "started": time.time() - 5,
     "resource": RES_RAW, "kind": "MHO", "host": "other-box"}), encoding="utf-8")
info = sl.touch(RES, kind="MHO")
check("同地址 holders 仍为 0（判重按地址，符合口径）", not info["holders"], f"holders={info['holders']}")
check("单独给出'跨接口未验证'告警", len(info["other_interfaces"]) == 1 and any(
    "另一接口" in w and "未验证" in w for w in info["warnings"]),
    (info["warnings"] or [""])[0][:90])

print("\n§5 过期 / 死进程的锁被清理", flush=True)
(tmp / "other_iface.json").unlink()
stale = tmp / "stale.json"
stale.write_text(json.dumps({"pid": child.pid, "ts": time.time() - (sl.LOCK_TTL_S + 60),
                             "resource": RES, "kind": "MHO"}), encoding="utf-8")
info = sl.touch(RES, kind="MHO")
check("过期锁已清理且不告警", not stale.exists() and not info["warnings"], f"exists={stale.exists()}")
child.terminate()
child.wait(timeout=10)
dead = tmp / "dead.json"
dead.write_text(json.dumps({"pid": child.pid, "ts": time.time(), "resource": RES, "kind": "MHO"}),
                encoding="utf-8")
info = sl.touch(RES, kind="MHO")
check("死进程锁已清理且不告警", not dead.exists() and not info["warnings"], f"exists={dead.exists()}")

print("\n§6 异常降级：锁失败绝不影响工具", flush=True)
# 制造"保证不可写"的目录：先建一个**同名文件**，再让锁目录指到它的子路径——
# 对文件做 mkdir 必然失败（FileExistsError/NotADirectoryError），跨盘符无关。
# （2026-10-09 修：原实现指向 Z:/definitely/not/writable，赌"Z: 盘不存在"——
#  本机恰有 Z: 且可写 → mkdir 成功 → 降级分支根本没被走到 → 恒 FAIL；
#  见 AGENTS.md 待办与 verify_session_lock 的既有问题记录。）
_blocker = tmp / "not_a_dir"                      # tmp 来自前文 tempfile.mkdtemp
_blocker.write_text("occupied by a file, not a dir", encoding="utf-8")
sl._locks_dir = lambda: _blocker / "child"        # type: ignore[assignment]
info = sl.touch(RES, kind="MHO")
check("目录不可写 → 返回空告警 + error 字段（不抛异常）",
      info["warnings"] == [] and "error" in info, str(info.get("error"))[:60])

print(f"\n== 结果: {'全部 PASS' if not fails else f'{len(fails)} 项 FAIL'} ==")
for f in fails:
    print(f"  - {f}")
sys.exit(1 if fails else 0)
