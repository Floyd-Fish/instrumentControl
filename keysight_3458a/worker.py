"""3458A I/O 的**独立进程 worker**（协议：stdin/stdout 一行一条 JSON）。

## 为什么必须独立进程（2026-09-23 实测，两条硬理由）

1. **两套 VISA 不能同进程**：MCP 是长驻进程，别的仪器工具调用 `pyvisa` 会把**系统 VISA**
   （`C:\\Windows\\system32\\visa32.dll`，IVI 壳）加载进来；此后本库的 Keysight VISA 通路
   （ctypes → `ktvisa32.dll`）就串味了：
   * 在干净进程里：`viOpen` 正常；
   * 一旦进程里装了系统 VISA：`viWrite` 报 `VI_ERROR_INV_OBJECT`；在 MCP 进程里更严重，
     `viOpen` 直接**访问违例**（`access violation reading 0x8`）。
   实测两种加载顺序（先 Keysight 后系统 / 反之）**都会坏**，所以只能**分进程**。
2. **卡在 DLL 内的调用无法从同进程中断**：`viOpen/viRead/viWrite` 一旦卡在 `ioGPIB` 里，
   Python 的超时/`finally` 都执行不到（线程卡在 C 调用内），接口被那个进程持续占着。
   独立进程后，父进程可以 `kill()` 它——Windows 会强制回收其全部句柄（这就是
   `docs/3458a_wedge_postmortem_20260923.md` §9 TODO A 的实现）。

## 协议

请求（父 → 子，一行 JSON）::

    {"id": 1, "op": "call", "method": "read_dcv", "kwargs": {"timeout_s": 15.0}}
    {"id": 2, "op": "get",  "attr": "current_nplc"}
    {"id": 3, "op": "close"}
    {"id": 4, "op": "__sleep", "seconds": 30}      # 仅供测试"硬截止 + kill"用

回复（子 → 父，一行 JSON）::

    {"id": 1, "ok": true,  "result": -2.27e-06}
    {"id": 1, "ok": false, "error": "TransportError: ..."}

方法/属性**白名单**在下面，未登记的一律拒绝（不执行任意代码）。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 允许远程调用的方法（= MCP 里 ks3458a_* 工具实际用到的驱动接口）
ALLOWED_METHODS = {
    "connect", "close", "recover", "prepare_for_read",
    "idn", "state", "error_string", "temperature",
    "read_dcv", "read_avg", "read_stats", "read_series", "read_acv", "read_burst",
    "configure_dcv", "set_range", "set_autorange", "set_nplc", "configure_acv",
    "snapshot_config", "restore_config", "release_to_front_panel",
    "reset", "unstick", "write", "query",
}
ALLOWED_ATTRS = {"current_nplc", "current_range", "resource", "holders",
                 "recover_mode", "ifc_path"}


def _reply(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> int:                                     # pragma: no cover - 子进程入口
    from keysight_3458a import DMM3458A

    drv = None
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception as exc:                                  # noqa: BLE001
            _reply({"id": None, "ok": False, "error": f"请求不是合法 JSON: {exc}"})
            continue
        rid = req.get("id")
        op = req.get("op")
        try:
            if op == "call":
                method = req.get("method") or ""
                if method not in ALLOWED_METHODS:
                    raise ValueError(f"方法不在白名单：{method!r}")
                if drv is None:
                    drv = DMM3458A(req.get("resource") or "GPIB0::9::INSTR",
                                    timeout_s=float(req.get("timeout_s") or 30.0))
                fn = getattr(drv, method)
                result = fn(**(req.get("kwargs") or {}))
                _reply({"id": rid, "ok": True, "result": result})
            elif op == "get":
                attr = req.get("attr") or ""
                if attr not in ALLOWED_ATTRS:
                    raise ValueError(f"属性不在白名单：{attr!r}")
                if drv is None:
                    raise RuntimeError("尚未连接（先 call connect）")
                _reply({"id": rid, "ok": True, "result": getattr(drv, attr)})
            elif op == "close":
                if drv is not None:
                    try:
                        drv.close()
                    except Exception:                             # noqa: BLE001
                        pass
                    drv = None
                _reply({"id": rid, "ok": True, "result": None})
                return 0                                          # 顺序关闭
            elif op == "__sleep":                                 # 仅测试用
                time.sleep(float(req.get("seconds") or 1.0))
                _reply({"id": rid, "ok": True, "result": "slept"})
            else:
                raise ValueError(f"未知 op：{op!r}")
        except Exception as exc:                                  # noqa: BLE001
            _reply({"id": rid, "ok": False, "error": f"{type(exc).__name__}: {exc}"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
