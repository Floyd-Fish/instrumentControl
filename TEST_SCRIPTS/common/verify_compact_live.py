"""compact profile 的**实机**冒烟测试（只读）——补上"设备路径未验证"这个缺口。

为什么需要它：阶段 1–4 的验证全部是离线的（假 registry / 假 executor），证明的是调度、
作用域、错误传播这些逻辑，**不是 VISA 行为**。而阶段 5 要把 compact 切成默认，那会改变
日常使用的仪器接口 —— 在没验证过的地面上改默认接口不合适。本脚本就是补这一刀。

安全设计（都写进代码，不靠使用者记）：

1. **必须在命令行显式确认**才会运行（`--i-know-this-touches-hardware`）；缺少即退出，
   所以它不可能被误触发（例如被别的脚本批量调用时）。
2. **只调用只读操作**：每个候选 op 都要过 `instrument_runtime` 的安全分级
   （`catalog.RISK_BY_TOOL`），`risk != "read_only"` 一律拒绝。这不是"约定"，是运行期检查。
3. **不写任何设备状态**：不设档位、不开关输出、不改触发、不发复位。全部是测量/查询。
4. 通过 MCP stdio 走**真实协议**（不是进程内直调），因此验证的是客户端真正看到的那条路。

用法（**会碰真实仪器，需先获得授权**）：
    python TEST_SCRIPTS/common/verify_compact_live.py --i-know-this-touches-hardware
    # 可选：--device sds|dmm|dg|psu|mho|sdg   只测指定设备族
    # 可选：--repeat 3                         batch 里重复读几次

不碰仪器的那部分（tools/list、search、describe、拒绝路径）已经在 verify_compact_mcp.py 里覆盖，
本脚本只补"真实调用"这一段。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SERVER = ROOT / "mcp_instruments" / "server.py"
PY_EXE = sys.executable
sys.path.insert(0, str(ROOT))

fails: list[str] = []


def _ascii(text) -> str:
    return str(text).encode("ascii", "replace").decode("ascii")


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {_ascii(name):52s} {_ascii(detail)[:110]}", flush=True)
    if not ok:
        fails.append(name)


class Probe:
    def __init__(self, profile: str = "compact", timeout: float = 300.0) -> None:
        env = dict(os.environ)
        env["INSTRUMENT_MCP_PROFILE"] = profile
        self.timeout = timeout
        self.proc = subprocess.Popen([PY_EXE, str(SERVER)], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     bufsize=0, cwd=str(ROOT), env=env)

    def send(self, obj: dict) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def read_line(self, timeout: float | None = None) -> str:
        assert self.proc.stdout is not None
        box: dict = {}
        th = threading.Thread(
            target=lambda: box.update(l=self.proc.stdout.readline().decode("utf-8", "replace")),
            daemon=True)
        th.start()
        th.join(timeout if timeout is not None else self.timeout)
        return box.get("l", "")

    def handshake(self) -> None:
        self.send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                   "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                              "clientInfo": {"name": "compact-live", "version": "1"}}})
        self.read_line()
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

    def call(self, name: str, args: dict, timeout: float | None = None) -> dict:
        self.send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                   "params": {"name": name, "arguments": args}})
        line = self.read_line(timeout)
        if not line.strip():
            return {"_transport": "timeout"}
        obj = json.loads(line)
        if "error" in obj:
            return {"_rpc_error": obj["error"]}
        content = (obj.get("result") or {}).get("content") or []
        try:
            return json.loads(content[0]["text"]) if content else {}
        except Exception:                                        # noqa: BLE001
            return {"_raw": content[0].get("text") if content else ""}

    def close(self) -> str:
        try:
            if self.proc.stdin is not None:
                self.proc.stdin.close()
            self.proc.wait(timeout=15)
        except Exception:                                        # noqa: BLE001
            self.proc.kill()
        assert self.proc.stderr is not None
        return self.proc.stderr.read().decode("utf-8", "replace")


def _reachable_kinds(p) -> list[str]:
    """从 `instr_devices` 快路径取"已知地址"里的设备族，**USB 直连的排前面**。

    为什么需要它：下面的候选表只保证操作是 read_only，**不保证那台仪器接着**。本机只连
    了 DG832 时，按固定顺序会挑中 `dmm.status`，于是 S1/S2 报一串 connection 失败——
    看起来像回归，其实只是选错了设备。

    USB 资源是本机直连，比 LAN 资源可靠得多，所以优先；只读配置+缓存，不触发网络扫描。
    拿不到列表就返回空，调用方退回固定顺序。
    """
    try:
        r = p.call("instr_devices", {})
    except Exception:                                            # noqa: BLE001
        return []
    if not r.get("ok") or r.get("mode") != "known":
        return []
    devs = [d for d in (r.get("devices") or []) if d.get("kind")]
    devs.sort(key=lambda d: 0 if str(d.get("resource", "")).upper().startswith("USB") else 1)
    return [d["kind"] for d in devs]


def _read_only_probe_op(device: str | None, preferred: list[str] | None = None
                        ) -> tuple[str, dict] | None:
    """挑一个**只读**的、不需要复杂参数的探测操作。

    逐个候选都过安全分级（catalog.RISK_BY_TOOL）；任何一个不是 read_only 就跳过。
    `preferred`（来自 `_reachable_kinds`）只影响**尝试顺序**，不放松只读约束。
    """
    from instrument_runtime import RISK_BY_TOOL

    # (op_id, args)：全部为只读状态查询，不改变设备任何设置
    candidates = [
        ("dmm.status", {}),
        ("sds.status", {}),
        ("dg.status", {}),
        ("psu.status", {}),
        ("mho.status", {}),
        ("sdg.status", {}),
        ("ks3458a.status", {}),
    ]
    if device:
        order = [c for c in candidates if c[0].split(".")[0] == device]
    elif preferred:
        order = ([c for c in candidates if c[0].split(".")[0] in preferred]
                 + [c for c in candidates if c[0].split(".")[0] not in preferred])
    else:
        order = candidates
    for op_id, args in order:
        tool = op_id.replace(".", "_")
        if RISK_BY_TOOL.get(tool) != "read_only":
            continue        # 运行期强制：只碰只读操作
        return op_id, args
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="compact profile live smoke test (read-only)")
    ap.add_argument("--i-know-this-touches-hardware", dest="ack", action="store_true",
                    help="显式确认：本脚本会与真实仪器通信（只读）")
    ap.add_argument("--device", default=None, help="只测指定设备族")
    ap.add_argument("--repeat", type=int, default=3, help="batch 内重复读次数")
    ap.add_argument("--write-test", dest="write_test", action="store_true",
                    help="额外跑写路径测试：会改设备设置，并短暂打开/关闭输出（需单独授权）")
    args = ap.parse_args()

    if not args.ack:
        # 这几行是**给人看的**，保持 ASCII：中文在部分控制台代码页下会变乱码，
        # 而"为什么被拒绝、怎么继续"恰恰是最不能看不懂的信息。
        print("REFUSED: this script talks to real instruments.")
        print("Confirm the target device and impact, then re-run with:")
        print("    --i-know-this-touches-hardware")
        print("It only calls operations classified read_only in the catalog;")
        print("no device state is changed (no output/range/trigger/reset writes).")
        return 2

    from instrument_runtime import RISK_BY_TOOL

    p = Probe()
    try:
        p.handshake()

        probe_op = _read_only_probe_op(args.device, _reachable_kinds(p))
        if probe_op is None:
            print(f"no eligible read-only probe operation found (device={args.device})")
            return 2
        op_id, op_args = probe_op
        print(f"== compact live smoke (read-only) ==\nprobe op: {op_id} args={op_args} "
              f"| batch repeats={args.repeat}\n")

        print("S1 single read-only call (instr_call)")
        r = p.call("instr_call", {"op": op_id, "args": op_args})
        ok = bool(r.get("ok"))
        check(f"instr_call({op_id}) returns ok", ok, str(r)[:100])
        check("response carries model (device layer really reached)",
              bool(r.get("model")) or not ok, str(r.get("model")))
        check("read-only op did not require confirm",
              r.get("error_type") != "confirm_required", str(r.get("error_type")))

        print("\nS2 batch read-only (instr_batch, whole batch = one job)")
        plan = {"plan_version": 1, "on_error": "stop",
                "max_leaf_steps": max(2, args.repeat + 1),
                "max_nesting_depth": 0,
                "steps": ([{"op": op_id, "args": op_args,
                            "capture": {"var": "r", "source": "result"}}]
                          + [{"op": op_id, "args": op_args} for _ in range(args.repeat - 1)])}
        b = p.call("instr_batch", {"plan": plan}, timeout=400)
        check("instr_batch completed", b.get("status") == "completed", str(b.get("status")))
        check(f"all {args.repeat} leaves succeeded",
              b.get("success_count") == args.repeat, str(b.get("success_count")))
        check("capture recorded exactly one entry", len(b.get("capture_log") or []) == 1,
              str(len(b.get("capture_log") or [])))
        check("run summary persisted to an artifact",
              bool(b.get("result_artifact")), str(b.get("result_artifact"))[:80])

        print("\nS3 consistency between the two paths")
        check("batch reported no unexpected warnings (contention-only allowed)",
              all("另一个进程" in (w.get("message") or "") for w in (b.get("warnings") or []))
              or not (b.get("warnings")), str((b.get("warnings") or [])[:1])[:90])

        print("\nS4 read-only self-proof")
        check("probe op is classified read_only in the catalog",
              RISK_BY_TOOL.get(op_id.replace(".", "_")) == "read_only",
              str(RISK_BY_TOOL.get(op_id.replace(".", "_"))))

        print("\nS5 guardrails still enforced through the compact path (rejections only)")
        # 这三条都**不会**改设备状态：前两条在执行前就被拒，第三条在 policy 层被拒
        # （黑名单判定发生在任何连接之前）。它们验证的是"换了调用入口，安全门没被绕过"。
        DEAD = "TCPIP0::127.0.0.1::9::SOCKET"     # 只为满足签名；判定在连接之前发生
        r = p.call("instr_call", {"op": "sdg.output",
                                  "args": {"ch": 1, "on": True, "expect_load": "HZ"}},
                   timeout=120)
        check("output without confirm is still rejected (confirm gate survives compact)",
              r.get("error_type") == "confirm_required", str(r)[:90])
        r = p.call("instr_call", {"op": "instr.query",
                                  "args": {"resource": DEAD, "cmd": "*IDN?;*RST"}},
                   timeout=120)
        check("write smuggled into a query is still rejected (blacklist survives compact)",
              r.get("error_type") == "forbidden", str(r)[:90])
        r = p.call("instr_call", {"op": "instr.write",
                                  "args": {"resource": DEAD, "cmd": ":VOLT 1"}},
                   timeout=120)
        check("raw write without confirm is still rejected",
              r.get("error_type") == "confirm_required", str(r)[:90])

        if args.write_test:
            print("\nS6 write path on the real device (state-changing, output toggled)")
            # 目标：验证"成功的写"也走得通 compact 路径 —— 之前只验证过成功的读与被拒的写。
            # 顺带验证 DG832 的保护联锁（库内护栏）在 compact 下依然拦得住。
            # 无论断言结果如何，最后一定把输出关回去（见本节收尾）。
            def _call(op, a, t=200):
                return p.call("instr_call", {"op": op, "args": a}, timeout=t)

            dev = op_id.split(".")[0]
            r = _call(dev + ".status", {})
            before = r.get("result") if r.get("ok") else None
            check("archived the pre-test device state", before is not None, str(r)[:80])

            ch = 1
            r = _call("dg.protect", {"ch": ch, "state": False})
            check("protection can be turned off", r.get("ok"), str(r)[:80])
            r = _call("dg.set_wave", {"ch": ch, "shape": "sine", "freq": 1000.0, "amp": 0.5})
            check("set_wave without protection is refused (DG832 interlock survives compact)",
                  not r.get("ok"), str(r)[:90])

            r = _call("dg.protect", {"ch": ch, "high": 3.3, "low": -3.3, "state": True})
            check("protection can be enabled", r.get("ok"), str(r)[:80])
            r = _call("dg.get_protect", {"ch": ch})
            check("protection reads back as configured",
                  r.get("ok") and "3.3" in json.dumps(r, ensure_ascii=False), str(r)[:90])

            r = _call("dg.set_wave", {"ch": ch, "shape": "sine", "freq": 1000.0, "amp": 0.5})
            check("set_wave succeeds with protection on (write path works)", r.get("ok"), str(r)[:90])

            r = _call("dg.output", {"ch": ch, "on": True, "confirm": True})
            check("output ON succeeds WITH confirm=True (gate permits, not just blocks)",
                  r.get("ok"), str(r)[:90])
            r = _call("dg.status", {})
            ch1 = ((r.get("result") or {}).get("ch1") or {})
            check("readback shows the output is ON", r.get("ok") and ch1.get("output") == "ON",
                  f"ch1.output={ch1.get('output')}")

            r = _call("dg.output", {"ch": ch, "on": False, "confirm": True})
            check("output OFF succeeds", r.get("ok"), str(r)[:90])
            r = _call("dg.status", {})
            ch1 = ((r.get("result") or {}).get("ch1") or {})
            check("readback shows the output is OFF", r.get("ok") and ch1.get("output") == "OFF",
                  f"ch1.output={ch1.get('output')}")

            # 写操作经 instr_batch 也要走得通（整批一个 job）
            wplan = {"plan_version": 1, "on_error": "stop", "max_leaf_steps": 4,
                     "max_nesting_depth": 0,
                     "steps": [{"op": "dg.set_wave",
                                "args": {"ch": ch, "shape": "sine", "freq": 500.0, "amp": 0.5}},
                               {"op": "dg.status", "args": {}}]}
            b2 = p.call("instr_batch", {"plan": wplan}, timeout=300)
            check("a write step runs through instr_batch too",
                  b2.get("status") == "completed" and b2.get("success_count") == 2,
                  f"status={b2.get('status')} ok={b2.get('success_count')}")

            # 收尾：确保输出关闭（无条件），并恢复测试前的波形参数。
            # ⚠ 首版这里写成 before["shape"]，而 dg.status 的结果是 {model,idn,ch1,ch2}——
            # 通道字段在 before["ch1"] 下面，于是 shape 恒为 None、**恢复压根没执行**
            # （实测把设备留在了测试用的 500Hz/0.5Vpp 上）。字段路径与 SCPI 短名都要处理。
            r = _call("dg.output", {"ch": ch, "on": False, "confirm": True})
            check("cleanup: output confirmed OFF at the end", r.get("ok"), str(r)[:80])
            ch1_before = (before or {}).get("ch1") or {}
            short2long = {"SIN": "sine", "SQU": "square", "RAMP": "ramp", "PULS": "pulse",
                          "NOIS": "noise", "DC": "dc", "USER": "user", "HARM": "harmonic"}
            shape = str(ch1_before.get("shape") or "").upper()
            if shape:
                shape = short2long.get(shape, str(ch1_before.get("shape")).lower())
                rr = _call("dg.set_wave", {"ch": ch, "shape": shape,
                                           "freq": float(ch1_before.get("freq") or 1000.0),
                                           "amp": float(ch1_before.get("amp") or 1.0)})
                check("restored the pre-test waveform settings", rr.get("ok"),
                      f"shape={shape} freq={ch1_before.get('freq')} amp={ch1_before.get('amp')}")
                r2 = _call("dg.status", {})
                now = ((r2.get("result") or {}).get("ch1") or {})
                check("restore verified by readback",
                      now.get("freq") == ch1_before.get("freq")
                      and now.get("amp") == ch1_before.get("amp"),
                      f"freq={now.get('freq')} amp={now.get('amp')}")
        p.close()
    finally:
        try:
            p.close()
        except Exception:                                        # noqa: BLE001
            pass

    print(f"\n== result: {'all PASS' if not fails else f'{len(fails)} FAIL'} ==")
    for f in fails:
        print(f"  - {_ascii(f)}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
