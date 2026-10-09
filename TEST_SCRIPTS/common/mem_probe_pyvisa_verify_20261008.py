# -*- coding: utf-8 -*-
"""独立复现：OPENBLAS_NUM_THREADS=1 对 `import pyvisa` 私有提交的影响。

来源口径：ChatSpace/TEST_SCRIPTS/mem_probe_pyvisa_20261008_0140.py（--threads1 模式），
复制到本仓以便独立复现，且**不覆盖** ChatSpace 侧的证据 JSON；输出写本仓 TEST_DATA
并带时间戳。

用法（两种模式各起一个**新进程**，numpy 已加载的进程测不出增量）：
    未修复：python mem_probe_pyvisa_verify_20261008.py --threads1
    已修复：$env:OPENBLAS_NUM_THREADS='1'; python mem_probe_pyvisa_verify_20261008.py --threads1

指标：PagefileUsage（= 私有提交 Private Commit，与需求文档口径一致）。
"""
import ctypes
import json
import os
import sys
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class PMC(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


_K32 = ctypes.windll.kernel32
_PROC = _K32.GetCurrentProcess()
_GETPMC = _K32.K32GetProcessMemoryInfo
_GETPMC.restype = wintypes.BOOL
_GETPMC.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]


def mem_mb():
    p = PMC()
    p.cb = ctypes.sizeof(PMC)
    if not _GETPMC(_PROC, ctypes.byref(p), p.cb):
        return -1.0, -1.0
    return p.PagefileUsage / 1048576, p.WorkingSetSize / 1048576


rows = []


def snap(label, note=""):
    commit, ws = mem_mb()
    delta = commit - (rows[-1]["commit_mb"] if rows else 0)
    rows.append({"step": label, "commit_mb": round(commit, 1),
                 "ws_mb": round(ws, 1), "delta": round(delta, 1), "note": note})
    print("%-46s commit=%7.1f (delta %+7.1f)  ws=%6.1f  %s"
          % (label, commit, delta, ws, note), flush=True)


def numpy_loaded():
    return "numpy" in sys.modules


env_threads = os.environ.get("OPENBLAS_NUM_THREADS")
snap("baseline OPENBLAS_NUM_THREADS=%r" % env_threads)
import pyvisa  # noqa: E402
snap("import pyvisa", "numpy_loaded=%s" % numpy_loaded())

# 顺带报告 numpy 实际看到的 BLAS 线程数（若 numpy 已加载）
try:
    import numpy  # noqa: F401
    snap("import numpy", "numpy=%s" % numpy.__version__)
except Exception as e:  # noqa: BLE001
    snap("import numpy", "FAIL %s" % str(e)[:60])

ts = time.strftime("%Y%m%d_%H%M%S")
tag = "threads1" if env_threads == "1" else "unfixed"
out_dir = str(ROOT / "TEST_DATA" / "common")
os.makedirs(out_dir, exist_ok=True)
out = os.path.join(out_dir, "mem_probe_pyvisa_%s_%s.json" % (tag, ts))
with open(out, "w", encoding="utf-8") as f:
    json.dump({
        "probe": "pyvisa import private-commit delta",
        "env_OPENBLAS_NUM_THREADS": env_threads,
        "mode": tag,
        "python": sys.version,
        "rows": rows,
        "ts": ts,
    }, f, ensure_ascii=False, indent=2)
print("saved:", out)
