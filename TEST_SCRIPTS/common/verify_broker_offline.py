"""Broker 层离线校验（不碰仪器）——方案 C 阶段 2 的验收判据。

阶段 2 的目标是让「黑名单 / 审计 / 锁 / 回读验证」**不依赖 MCP wrapper**，同时
**行为逐字不变**。两组断言分别证明这两件事：

    S1 依赖隔离：单独 import instrument_runtime.broker 不得牵入 mcp / fastmcp /
       pyvisa（在**子进程**里查 sys.modules，避免被本进程已加载的模块污染）。
    S2 判据等价：broker.decide_scpi 的结果必须与迁移前的参考表达式
       （`is_forbidden(cmd) or not is_query_only(cmd)` / `is_forbidden(cmd)`）
       在语料上逐条一致。
    S3 结构等价：server.py 的私有名必须是 runtime 里**同一个函数对象**
       （证明是委托，不是又抄了一份实现）。
    S4 锁唯一：server._DEVICE_LOCK 必须**就是** broker.DEVICE_LOCK 这个对象
       （两个不同的 Lock 会让第二道防线形同虚设）。
    S5 行为向量：pair_readback / drain_errors / audit 的既有行为逐条钉住。

用法：python TEST_SCRIPTS/common/verify_broker_offline.py
（输出纯 ASCII，任何控制台代码页都能读）
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "mcp_instruments"))

fails: list[str] = []


def _ascii(text) -> str:
    return str(text).encode("ascii", "replace").decode("ascii")


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {_ascii(name):56s} {_ascii(detail)[:110]}", flush=True)
    if not ok:
        fails.append(name)


# 判据语料：覆盖"合法查询 / 写 / 混合 / 复位族 / 锁定族 / 中间缩写 / 空串"。
# 中间缩写那几条是本仓历史上真漏网过的写法（见 policy.py 注释），必须留在语料里。
CORPUS = [
    # 合法纯查询
    ":VOLT:DC?", ":MEASure:ITEM? VPP,CHANnel1", ":TRIGger:EDGE:LEVel? MAX",
    ":CHANnel4:DISPlay?;:CHANnel4:SCALe?", "*IDN?", ":SYST:REM?", ":SYSTem:LOCKed?",
    "C1:BSWV WVTP?", ":SOUR1:APPL?",
    # 写命令
    ":OUTP1 ON", "VOLT 1", ":SOUR1:PHAS 123", ":SYST:REM ON", ":SYST:LOCK 1",
    # 混合消息（查询口最危险的形态）
    ":OUTP1 ON;:OUTP1?", "*IDN?;*RST", ":CHANnel4:DISPlay?;:OUTP4 ON",
    ":SOUR1:PHAS?;:SOUR1:PHAS 123",
    # 复位/存储覆写族（一律拦）
    "*RST", "*SAV 1", "*RCL 1", ":SYST:RES", ":SYST:RESE", ":SYSTEM:RESET",
    ":SYST:PRES", ":SYST:PRESE", ":SYST:FACT", ":SYST:FACTORY",
    # 锁定族（只拦写）
    ":SYST:COMM:RLST RWL", ":SYST:COMMU:RLST RWL", ":SYST:REMON", ":SYST:RWL",
    # 边界
    "", "  ", ";", ":", "?", ":CH1:DISP?;",
]


def main() -> int:
    # ---------------- S1 依赖隔离（子进程）----------------
    print("S1 broker imports without the MCP wrapper (subprocess check)")
    probe = (
        "import sys; sys.path.insert(0, r'%s'); "
        "import instrument_runtime.broker as b; "
        "bad=[m for m in ('mcp','fastmcp','pyvisa','pyvisa_py') if m in sys.modules]; "
        "print('BAD=' + ','.join(bad))" % str(ROOT)
    )
    r = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=120)
    line = (r.stdout or "").strip().splitlines()
    bad = line[-1] if line else "(no output)"
    check("importing instrument_runtime.broker pulls no MCP/VISA modules",
          bad == "BAD=", bad)

    # ---------------- S2 判据等价 ----------------
    print("\nS2 decide_scpi matches the pre-refactor predicate expressions")
    from instrument_runtime import broker
    from instrument_runtime.policy import is_forbidden, is_query_only

    mism_query, mism_write, reasons = [], [], {}
    for cmd in CORPUS:
        ref_query_allowed = not (is_forbidden(cmd) or not is_query_only(cmd))
        ref_write_allowed = not is_forbidden(cmd)
        dq = broker.decide_scpi(cmd, require_query=True)
        dw = broker.decide_scpi(cmd, require_query=False)
        if dq.allowed != ref_query_allowed:
            mism_query.append((cmd, dq.allowed, ref_query_allowed))
        if dw.allowed != ref_write_allowed:
            mism_write.append((cmd, dw.allowed, ref_write_allowed))
        if not dq.allowed:
            reasons[dq.reason] = reasons.get(dq.reason, 0) + 1
    check("query-channel decisions match the reference expression", not mism_query,
          f"mismatches {mism_query[:3]}" if mism_query else f"{len(CORPUS)} commands ok")
    check("write-channel decisions match the reference expression", not mism_write,
          f"mismatches {mism_write[:3]}" if mism_write else f"{len(CORPUS)} commands ok")
    check("denied query reasons are classified", set(reasons) <= {"not_query", "forbidden_reset",
                                                                  "forbidden_lock"},
          str(reasons))
    # 参考实现必须仍能拦下历史漏网写法（防止语料整体失效）
    check("reference still blocks the historic mid-abbreviation resets",
          all(is_forbidden(c) for c in (":SYST:RESE", ":SYST:PRESE", ":SYST:COMMU:RLST RWL")),
          "ok")

    # ---------------- S3 结构等价 + S4 锁唯一 ----------------
    print("\nS3 server delegates to runtime (same function objects)")
    import server as S  # noqa: E402

    from instrument_runtime import policy, verify as verify_mod

    for priv, pub, mod in (
        ("_is_forbidden", "is_forbidden", policy),
        ("_is_query_only", "is_query_only", policy),
        ("_classify_forbidden", "classify_forbidden", policy),
        ("_drain_errors", "drain_errors", verify_mod),
        ("_pair_readback", "pair_readback", verify_mod),
    ):
        check(f"server.{priv} is runtime.{pub}", getattr(S, priv) is getattr(mod, pub))

    print("\nS4 the device lock is a single shared object")
    check("server._DEVICE_LOCK is broker.DEVICE_LOCK", S._DEVICE_LOCK is broker.DEVICE_LOCK,
          f"server={id(S._DEVICE_LOCK)} broker={id(broker.DEVICE_LOCK)}")
    check("lock is a plain (non-reentrant) lock",
          type(broker.DEVICE_LOCK).__name__ == "lock", type(broker.DEVICE_LOCK).__name__)

    # ---------------- S5 行为向量 ----------------
    print("\nS5 behaviour vectors for the moved helpers")
    pr = broker.pair_readback
    check("pair_readback: single command -> None", pr(":VOLT?", "1.0") is None)
    check("pair_readback: multi command pairs by position",
          pr(":C1:SCALe?;:C1:OFFSet?", "1.0;0.5") == [
              {"cmd": ":C1:SCALe?", "value": "1.0"}, {"cmd": ":C1:OFFSet?", "value": "0.5"}])
    seg = pr(":A?;:B?", "only-one")
    check("pair_readback: segment mismatch is reported, not guessed",
          isinstance(seg, list) and "note" in seg[0] and "段数不符" in seg[0]["note"],
          _ascii(seg))
    check("pair_readback: empty/None inputs -> None",
          pr(None, "x") is None and pr("", "x") is None and pr(":A?", None) is None)

    class _Fake:
        """Minimal SYST:ERR? responder for drain_errors."""

        def __init__(self, seq):
            self.seq = list(seq)

        def query(self, _cmd):
            return self.seq.pop(0) if self.seq else "+0,\"No error\""

    check("drain_errors: clean queue -> []", broker.drain_errors(_Fake(["+0,\"No error\""])) == [])
    check("drain_errors: collects until clean",
          broker.drain_errors(_Fake(["-113,\"Undefined header\"", "+0,\"No error\""])) ==
          ["-113,\"Undefined header\""])
    check("drain_errors: caps at 20 entries",
          len(broker.drain_errors(_Fake(["-100,\"x\""] * 40))) == 21,
          "20 errors + cap notice")

    with tempfile.TemporaryDirectory() as tmp:
        p = broker.audit_scpi("instr_write", "USB0::FAKE", "*IDN?", refused="forbidden",
                              root=tmp)
        pf = Path(p)
        ok_name = pf.name.startswith("mcp_scpi_audit_") and pf.suffix == ".jsonl"
        entry = json.loads(pf.read_text(encoding="utf-8").strip().splitlines()[-1])
        check("audit_scpi: filename pattern and JSONL fields",
              ok_name and entry["tool"] == "instr_write" and entry["cmd"] == "*IDN?"
              and entry["refused"] == "forbidden" and "ts" in entry,
              f"{pf.name} {entry.get('tool')}")

    print(f"\n== result: {'all PASS' if not fails else f'{len(fails)} FAIL'} ==")
    for f in fails:
        print(f"  - {_ascii(f)}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
