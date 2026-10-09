# -*- coding: utf-8 -*-
"""前板交接三件套的离线自检（不碰真机：mock transport）。

S1 snapshot_config：查询全部发出、字段解码正确
S2 restore_config：按快照写回 + 回读校验（含"期望不符"路径）
S3 release_to_front_panel：TARM/TRIG AUTO + ARANGE ON + 状态回读 issues
S4 close(release=True) 走释放分支 / 默认走 TARM HOLD
S5 白名单包含三个新方法（worker 远程通道可达）
"""
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from keysight_3458a import commands as C          # noqa: E402
from keysight_3458a.dmm3458a import DMM3458A      # noqa: E402
from keysight_3458a import worker as W            # noqa: E402

results = []


def check(name, cond, extra=""):
    results.append((name, bool(cond)))
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {extra}")


class MockTransport:
    """记录全部写命令；查询按脚本回放。"""

    def __init__(self, answers: dict):
        self.writes = []
        self.answers = dict(answers)

    def write(self, cmd):
        self.writes.append(cmd)

    def query(self, cmd):
        if cmd in self.answers:
            v = self.answers[cmd]
            if callable(v):
                v = v(self)
            return v
        raise AssertionError(f"mock 未准备回答: {cmd}")

    def close(self):
        self.writes.append("<close>")


def make_dmm(answers):
    t = MockTransport(answers)
    d = object.__new__(DMM3458A)          # 绕过 __init__（不连真机）
    d._transport = t
    d._connected = True
    d.timeout_s = 30.0
    d._range = None
    d._nplc = None
    d._autorange = None
    return d, t


# --- S1 snapshot ---
d, t = make_dmm({
    C.FUNC_Q: "1, .1", C.RANGE_Q: "10", C.NPLC_Q: "10",
    C.ARANGE_Q: "0", C.AZERO_Q: "1",
})
snap = d.snapshot_config()
check("S1 snapshot 字段", snap == {"function": "1, .1", "range_v": 10.0,
                                   "nplc": 10.0, "arange": 0, "azero": 1}, str(snap))

# --- S2 restore（成功路径 + 校验失败路径）---
d, t = make_dmm({
    C.DCV: None, C.NPLC: None, "AZERO ON": None, C.ARANGE_ON: None,
    C.FUNC_Q: "1, .1", C.RANGE_Q: "10", C.NPLC_Q: "10",
    C.ARANGE_Q: "1", C.AZERO_Q: "1",
})
issues = d.restore_config({"function": "1, .1", "range_v": 10.0,
                           "nplc": 10.0, "arange": 1, "azero": 1})
check("S2a restore 写序列", [w for w in t.writes if w != "<close>"] ==
      ["DCV 10", "NPLC 10", "AZERO ON", "ARANGE ON"], str(t.writes))
check("S2b restore 回读一致 → 无 issues", issues == [], str(issues))

d, t = make_dmm({
    C.DCV: None, C.NPLC: None, C.ARANGE_ON: None,
    C.FUNC_Q: "1, .1", C.RANGE_Q: "100", C.NPLC_Q: "10",   # RANGE 回读不符
    C.ARANGE_Q: "1", C.AZERO_Q: "1",
})
issues = d.restore_config({"function": "1, .1", "range_v": 10.0,
                           "nplc": 10.0, "arange": 1, "azero": 1})
check("S2c 回读不符 → 记 issue", len(issues) == 1 and "range_v" in issues[0], str(issues))

# --- S3 release ---
d, t = make_dmm({
    C.TARM_AUTO: None, C.TRIG_AUTO: None, C.ARANGE_ON: None,
    C.ID: "HP3458A", C.ERRSTR: '0,"NO ERROR"', C.TEMP: "39.1",
    C.TARM_Q: "1", C.TRIG_Q: "1", C.NRDGS_Q: "1, 1", C.NPLC_Q: "10",
    C.APER_Q: "0", C.FUNC_Q: "1, .1", C.RANGE_Q: "10", C.ARANGE_Q: "1",
    C.AZERO_Q: "1", C.MFORMAT_Q: "1", C.OFORMAT_Q: "1", C.END_Q: "2",
    C.INBUF_Q: "1",
})
issues = d.release_to_front_panel()
check("S3a release 写序列", t.writes == [C.TARM_AUTO, C.TRIG_AUTO, C.ARANGE_ON], str(t.writes))
check("S3b release 无 issues", issues == [], str(issues))

d, t = make_dmm({
    C.TARM_AUTO: None, C.TRIG_AUTO: None, C.ARANGE_ON: None,
    C.ID: "HP3458A", C.ERRSTR: '0,"NO ERROR"', C.TEMP: "39.1",
    C.TARM_Q: "4", C.TRIG_Q: "4",                    # 仍 HOLD → 应记 issues
    C.NRDGS_Q: "1, 1", C.NPLC_Q: "10", C.APER_Q: "0",
    C.FUNC_Q: "1, .1", C.RANGE_Q: "10", C.ARANGE_Q: "0", C.AZERO_Q: "1",
    C.MFORMAT_Q: "1", C.OFORMAT_Q: "1", C.END_Q: "2", C.INBUF_Q: "1",
})
issues = d.release_to_front_panel()
check("S3c HOLD 未解除 → 3 条 issue（tarm/trig/arange）",
      len(issues) == 3, str(issues))

# --- S4 close 分支 ---
d, t = make_dmm({C.TARM_HOLD: None})
d.close()
check("S4a close 默认 TARM HOLD", t.writes == [C.TARM_HOLD, "<close>"], str(t.writes))

d, t = make_dmm({
    C.TARM_AUTO: None, C.TRIG_AUTO: None, C.ARANGE_ON: None,
    C.ID: "HP3458A", C.ERRSTR: '0,"NO ERROR"', C.TEMP: "39.1",
    C.TARM_Q: "1", C.NRDGS_Q: "1, 1", C.NPLC_Q: "10", C.APER_Q: "0",
    C.FUNC_Q: "1, .1", C.RANGE_Q: "10", C.ARANGE_Q: "1", C.AZERO_Q: "1",
    C.MFORMAT_Q: "1", C.OFORMAT_Q: "1", C.END_Q: "2", C.INBUF_Q: "1",
})
d.close(release=True)
check("S4b close(release=True) 走 AUTO 序列", t.writes[:3] ==
      [C.TARM_AUTO, C.TRIG_AUTO, C.ARANGE_ON], str(t.writes))

# --- S5 白名单 ---
check("S5 白名单含三件套",
      {"snapshot_config", "restore_config", "release_to_front_panel"} <= W.ALLOWED_METHODS)

fails = [n for n, ok in results if not ok]
print(f"\n== {len(results) - len(fails)}/{len(results)} PASS ==")
sys.exit(1 if fails else 0)
