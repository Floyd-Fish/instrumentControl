# -*- coding: utf-8 -*-
"""spawn_detached.py 的自测（不依赖 server.py）。

S1: 起一个 5s 的 python 子进程（写文件作证据），launcher 应立即返回 pid 并退出
S2: pid 存活、无 console 依附（父进程即退，子进程仍在跑）
S3: 子进程 stdout/stderr 重定向到指定文件
"""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parents[2] / "mcp_instruments"
LAUNCHER = HERE / "spawn_detached.py"
PY = sys.executable

tmp = Path(tempfile.mkdtemp(prefix="spawn_test_"))
out_log, err_log = tmp / "out.log", tmp / "err.log"
marker = tmp / "marker.txt"

child = tmp / "sleeper.py"
child.write_text(
    "import sys, time\n"
    f"open(r'{marker}', 'w').write('alive')\n"
    "print('stdout-line', file=sys.stdout, flush=True)\n"
    "print('stderr-line', file=sys.stderr, flush=True)\n"
    "time.sleep(5)\n", encoding="utf-8")

results = []


def check(name, cond, extra=""):
    results.append((name, bool(cond)))
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {extra}")


t0 = time.time()
r = subprocess.run([PY, str(LAUNCHER), PY, str(child), str(out_log), str(err_log)],
                   capture_output=True, text=True, timeout=30)
launcher_s = time.time() - t0
check("S1 launcher 秒退（<3s）", launcher_s < 3, f"{launcher_s:.2f}s rc={r.returncode}")
try:
    pid = int(r.stdout.strip())
    check("S1b 返回的是数字 pid", pid > 0, f"pid={pid}")
except ValueError:
    check("S1b 返回的是数字 pid", False, f"stdout={r.stdout!r} stderr={r.stderr!r}")
    pid = -1

time.sleep(1.0)
import psutil
alive = psutil.pid_exists(pid)
check("S2 子进程独立存活", alive)
check("S2b marker 已写（代码真的在跑）", marker.exists())
check("S3 stdout 重定向", out_log.exists() and "stdout-line" in out_log.read_text())
check("S3b stderr 重定向", err_log.exists() and "stderr-line" in err_log.read_text())

if pid > 0:
    psutil.Process(pid).kill()

fails = [n for n, ok in results if not ok]
print(f"\n== {len(results) - len(fails)}/{len(results)} PASS ==")
sys.exit(1 if fails else 0)
