"""量 instrument MCP 服务器**自身**的启动耗时（隔离出它对整体启动慢的贡献）。

分段：
  T1 进程 spawn → initialize 应答（含 python 启动、pyvisa/numpy 导入、VISA 预热线程启动）
  T2 initialize → tools/list 应答（工具登记与 schema 生成）
  T3 进程 spawn → 进程实际退出（收尾）

只读：不调用任何设备工具。
用法：python TEST_SCRIPTS/common/measure_mcp_startup.py [--repeat 2]
"""
from __future__ import annotations

import argparse
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


def _read_line(proc, timeout: float) -> str:
    box: dict = {}

    def _r():
        assert proc.stdout is not None
        box["l"] = proc.stdout.readline().decode("utf-8", "replace")

    th = threading.Thread(target=_r, daemon=True)
    th.start()
    th.join(timeout)
    return box.get("l", "")


def run_once(profile: str) -> dict:
    env = dict(os.environ)
    env["INSTRUMENT_MCP_PROFILE"] = profile
    t0 = time.perf_counter()
    proc = subprocess.Popen([PY_EXE, str(SERVER)], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            bufsize=0, cwd=str(ROOT), env=env)
    out: dict = {"profile": profile}
    try:
        proc.stdin.write((json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "startup-timer", "version": "1"}}}) + "\n")
            .encode("utf-8"))
        proc.stdin.flush()
        line = _read_line(proc, 180)
        out["t1_initialize_s"] = round(time.perf_counter() - t0, 2)
        if not line.strip():
            out["error"] = "initialize timed out"
            return out
        proc.stdin.write((json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized",
                                      "params": {}}) + "\n").encode("utf-8"))
        proc.stdin.write((json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list",
                                      "params": {}}) + "\n").encode("utf-8"))
        proc.stdin.flush()
        line2 = _read_line(proc, 180)
        out["t2_tools_s"] = round(time.perf_counter() - t0, 2)
        if line2.strip():
            out["tool_count"] = len(json.loads(line2)["result"]["tools"])
    finally:
        t_exit0 = time.perf_counter()
        try:
            proc.stdin.close()
            proc.wait(timeout=30)
        except Exception:                                        # noqa: BLE001
            proc.kill()
        out["t3_exit_s"] = round(time.perf_counter() - t0, 2)
        err = proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
        out["selfcheck"] = [l.strip() for l in err.splitlines() if "startup self-check" in l]
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeat", type=int, default=2)
    args = ap.parse_args()

    print(f"{'profile':9s} {'run':>3s} {'spawn->init':>11s} {'spawn->tools':>12s} "
          f"{'tools':>6s}")
    results = []
    for profile in ("compact", "legacy"):
        for i in range(1, args.repeat + 1):
            r = run_once(profile)
            results.append(r)
            print(f"{profile:9s} {i:3d} {r.get('t1_initialize_s', -1):11.2f} "
                  f"{r.get('t2_tools_s', -1):12.2f} {r.get('tool_count', -1):6d}", flush=True)
    print()
    for profile in ("compact", "legacy"):
        rs = [r for r in results if r["profile"] == profile and "t2_tools_s" in r]
        if rs:
            best = min(r["t2_tools_s"] for r in rs)
            worst = max(r["t2_tools_s"] for r in rs)
            print(f"{profile:9s} spawn->tools: {best:.2f}s .. {worst:.2f}s")
    sc = [r for r in results if r.get("selfcheck")]
    if sc:
        print("\nself-check 首行（证明它不碰设备）:")
        print("  " + sc[0]["selfcheck"][0][:160])
    return 0


if __name__ == "__main__":
    sys.exit(main())
