# -*- coding: utf-8 -*-
"""`instr_discover` 耗时分解探针（一次性分析，2026-09-23）。

背景：真机只读验收里 `instr_discover` 实测 190.2s，需要定位耗时到底花在
哪个阶段。本脚本**不改任何代码**，只复用 `common/discovery.py` 的现有函数
分阶段计时，复现 server 实际走的路径。

副作用与 `instr_discover` 相同：会扫本机私网网段、会探测串口。
留痕：TEST_DATA/common/profile_discovery_<stamp>.txt
"""
from __future__ import annotations

import ipaddress
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from common import discovery as d  # noqa: E402

OUT = ROOT / "TEST_DATA" / "common"
lines: list[str] = []


def say(s: str = "") -> None:
    print(s, flush=True)
    lines.append(s)


def tic() -> float:
    return time.perf_counter()


def main() -> int:
    say(f"=== instr_discover 耗时分解 {datetime.now():%Y-%m-%d %H:%M:%S} ===")
    say("")
    say("--- 参数 ---")
    say(f"  SCAN_PROBE_PORTS        = {d.SCAN_PROBE_PORTS}")
    say(f"  PROBE_TIMEOUT_S         = {d.PROBE_TIMEOUT_S}")
    say(f"  PROBE_WORKERS           = {d.PROBE_WORKERS}")
    say(f"  PROBE_ALIVE_CONCURRENCY = {d.PROBE_ALIVE_CONCURRENCY}")
    say(f"  PROBE_CHUNK             = {d.PROBE_CHUNK}")
    say(f"  SCAN_WORKERS/SCAN_TMO   = {d.SCAN_WORKERS} / {d.SCAN_TIMEOUT_MS}ms")
    say(f"  SERIAL_PROBE_TIMEOUT_S  = {d.SERIAL_PROBE_TIMEOUT_S}")

    say("")
    say("--- 阶段 0：网段选择 ---")
    t0 = tic()
    segs = d.local_scan_segments()
    say(f"  local_scan_segments() 耗时 {tic()-t0:.2f}s")
    say(f"  返回 {len(segs)} 个网段（由窄到宽）：")
    per_seg: dict[str, list[str]] = {}
    for s in segs:
        addrs = [str(h) for h in ipaddress.ip_network(s, strict=False).hosts()]
        per_seg[s] = addrs
        plen = ipaddress.ip_network(s, strict=False).prefixlen
        flag = "  <== 宽网段，地址数爆炸" if len(addrs) > 1024 else ""
        say(f"    {s:22s} /{plen:<2d} {len(addrs):6d} 个地址{flag}")
    total = sum(len(v) for v in per_seg.values())
    say(f"  合并后 all_addrs 总计 {total} 个地址")
    say("  ! server.py 把这些**合并成一次预筛**（不是逐段扫）")

    say("")
    say("--- 阶段 1：逐段端口预筛（probe_open_ports，server 实际用合并版）---")
    seg_cost: dict[str, float] = {}
    seg_alive: dict[str, int] = {}
    for s, addrs in per_seg.items():
        t0 = tic()
        op = d.probe_open_ports(addrs)
        dt = tic() - t0
        alive = [a for a, ports in op.items() if ports]
        seg_cost[s] = dt
        seg_alive[s] = len(alive)
        say(f"    {s:22s} {dt:7.1f}s   存活 {len(alive):5d}")
    sum_seg = sum(seg_cost.values())
    say(f"  逐段串行总计 {sum_seg:.1f}s（≈ server 合并扫的总量，同样受并发限流）")

    say("")
    say("--- 阶段 2：合并一次性预筛（复现 server 行为）---")
    all_addrs = [a for v in per_seg.values() for a in v]
    t0 = tic()
    op_all = d.probe_open_ports(all_addrs)
    dt_all = tic() - t0
    alive_all = [a for a, ports in op_all.items() if ports]
    say(f"  合并预筛 {dt_all:.1f}s   存活 {len(alive_all)}")

    say("")
    say("--- 阶段 3：VISA 资源列举 + 串口探测 ---")
    t0 = tic()
    res = d.list_resources()
    dt_list = tic() - t0
    serial = [r for r in res if r.upper().startswith("ASRL")]
    non_serial = [r for r in res if not r.upper().startswith("ASRL")]
    say(f"  list_resources {dt_list:.1f}s -> {len(res)} 个资源（串口 {len(serial)} / 非串口 {len(non_serial)}）")
    say(f"  已知挂起缓存 _HANGING_PORTS = {sorted(d._HANGING_PORTS)}")
    t0 = tic()
    sr = d.probe_serials_isolated(serial)
    dt_ser = tic() - t0
    say(f"  串口探测 {dt_ser:.1f}s（hard_timeout={d.SERIAL_PROBE_TIMEOUT_S}s/口，workers=4，挂起口跳过）")
    for e in sr:
        note = str(e.get("note") or "")
        say(f"    {e['resource']:20s} idn={str(e['idn'])[:38]:38s} {note[:34]}")

    say("")
    say("--- 阶段 4：非串口 VISA 识别 ---")
    t0 = tic()
    try:
        na = d.identify_all(non_serial)
        dt_na = tic() - t0
        say(f"  identify_all {dt_na:.1f}s")
        for e in na:
            if isinstance(e, dict):
                say(f"    {str(e.get('resource'))[:38]:38s} idn={str(e.get('idn'))[:38]}")
    except Exception as e:  # noqa: BLE001
        dt_na = tic() - t0
        say(f"  identify_all 异常（{type(e).__name__}: {e}）耗时 {dt_na:.1f}s")

    say("")
    say("--- 阶段 5：LAN 候选识别 ---")
    t0 = tic()
    try:
        found = d.identify_lan_all(alive_all, timeout_ms=1500, open_ports=op_all)
        dt_lan = tic() - t0
        say(f"  identify_lan_all {dt_lan:.1f}s  命中 {len(found)}")
        for k, v in list(found.items())[:10]:
            say(f"    {k:18s} -> {str(v)[:60]}")
    except Exception as e:  # noqa: BLE001
        dt_lan = tic() - t0
        say(f"  identify_lan_all 异常（{type(e).__name__}: {e}）耗时 {dt_lan:.1f}s")

    say("")
    say("=== 汇总（复现 server 路径）===")
    say(f"  {'合并预筛':<24s} {dt_all:8.1f}s   ({dt_all/(dt_all+dt_ser+dt_na+dt_lan)*100:4.0f}%)")
    say(f"  {'串口探测':<24s} {dt_ser:8.1f}s")
    say(f"  {'非串口 VISA 识别':<24s} {dt_na:8.1f}s")
    say(f"  {'LAN 候选识别':<24s} {dt_lan:8.1f}s")
    say(f"  {'合计':<24s} {dt_all+dt_ser+dt_na+dt_lan:8.1f}s")
    say("")
    say("  分网段预筛明细（用于判断该不该扫宽网段）：")
    for s, dt in sorted(seg_cost.items(), key=lambda x: -x[1]):
        plen = ipaddress.ip_network(s, strict=False).prefixlen
        say(f"    {s:22s} /{plen:<2d} {dt:7.1f}s  存活 {seg_alive[s]}")

    OUT.mkdir(parents=True, exist_ok=True)
    f = OUT / f"profile_discovery_{datetime.now():%Y%m%d_%H%M%S}.txt"
    f.write_text("\n".join(lines), encoding="utf-8")
    say("")
    say(f"留痕: {f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
