"""SCPI 策略层——黑名单与查询判据（纯函数，零外部依赖）。

方案 C 阶段 2（见 docs/gpt_qa/2026-09-23-instrument-gateway-arch.md）：把护栏从
`mcp_instruments/server.py` 里抽出来，使**不依赖 MCP/FastMCP/pyvisa** 就能复用——
compact profile、CLI、代码运行时都必须走同一个判据，否则"某个入口忘了拦"就是安全洞。

本模块的代码与注释**逐字迁移**自 server.py 的同名实现（2026-09-23），未做行为改动：
那些注释记录的是踩过的坑（误拦带参数查询、多段回读、助记符中间缩写漏网），
比代码本身更值钱，迁移时一律保留。
"""
from __future__ import annotations

import re

__all__ = [
    "FORBIDDEN_COMMON_RE",
    "RESET_NODES",
    "LOCK_NODES",
    "COMM_NODES",
    "RLST_NODES",
    "QUERY_UNIT_RE",
    "mnemonic",
    "any_node",
    "classify_forbidden",
    "is_query_only",
    "is_forbidden",
]

# 复位/存储覆写类黑名单（AGENTS.md 安全红线）：confirm=True 也不放行——
# 需显式授权的复位场景走测试脚本（如 dh1766 --allow-rst），不经 MCP。
FORBIDDEN_COMMON_RE = re.compile(r"\*(RST|SAV|RCL)")  # *RST / *SAV n / *RCL n

# 子系统助记符表：(短形式, 长形式)。SCPI 允许短形式与长形式之间的**任意前缀**
# （SCPI-99 §6.2.2 命令助记符），只比对两种写法会漏掉中间缩写
# （实测漏网：`:SYST:RESE`、`:SYST:PRESE`、`:SYST:COMMU:RLST RWL`）。
RESET_NODES = (("RES", "RESET"), ("FACT", "FACTORY"), ("PRES", "PRESET"))
LOCK_NODES = (("REM", "REMOTE"), ("RWL", "RWL"), ("LOCK", "LOCKED"))
COMM_NODES = (("COMM", "COMMUNICATE"),)
RLST_NODES = (("RLS", "RLSTATE"),)

# 单条命令单元的形状：命令头以 `?` 结尾，问号后**允许**带参数。
#
# `?` 后带参数是标准 SCPI 写法（`:MEASure:ITEM? VPP,CHANnel2`、`SAMPle:COUNt? MAX`），
# 故不能按"整段以 ? 结尾"判（2026-09-15 曾因此把带参数查询全拒了，用户报障后修正）。
QUERY_UNIT_RE = re.compile(r"^[:*]?[A-Za-z][A-Za-z0-9:<>{}_.]*\?(?:\s[\s\S]*)?$")


def mnemonic(token: str, short: str, long: str) -> bool:
    """SCPI 助记符匹配（宽松，黑名单用「宁可误拦」的偏置）。

    接受三类写法：① short..long 之间的任意前缀（SCPI-99 §6.2.2）；② 长形式本身；
    ③ 短形式开头后粘连参数（如 `SYST:REMON` = `SYST:REM ON`，历史实现按正则前缀
    搜索能拦下，行为必须保持）。
    """
    t, lo = token.upper(), long.upper()
    return len(t) >= len(short) and (lo.startswith(t) or t.startswith(short))


def any_node(token: str, pairs: tuple[tuple[str, str], ...]) -> bool:
    return any(mnemonic(token, s, l) for s, l in pairs)


def classify_forbidden(cmd: str) -> str | None:
    """逐条（`;` 分段）判定命令是否命中黑名单；命中返回类别，否则 None。

    必须在**分段**上判定：`*RST;*IDN?` 这类多命令消息单看整串会漏判，
    只看首段又会漏掉后续段（历史缺陷：instr_query 只查 `?` 不看黑名单，
    `"*IDN?;:SYST:RESE"` 可直接复位仪器）。
    """
    for part in cmd.split(";"):
        # 先切出命令头（空格前）再归一——否则 "SYST:REM ON" 归一成 "SYST:REMON"，
        # 参数会粘连到助记符上导致漏判。
        stripped = part.strip()
        if not stripped:
            continue
        head = re.split(r"\s+", stripped)[0].upper().lstrip(":")
        if not head:
            continue
        # 公共命令族（*RST/*SAV/*RCL）在**整段**上搜，不限定在命令头——数据段里混进
        # 这几个词没有正当用途，宁可误拦（防御"参数位置偷发复位"）。
        if FORBIDDEN_COMMON_RE.search(stripped):
            return "reset"
        segs = [s for s in head.split(":") if s]
        if not segs or not mnemonic(segs[0], "SYST", "SYSTEM"):
            continue
        if len(segs) >= 2 and any_node(segs[1], RESET_NODES):
            return "reset"
        if len(segs) >= 2 and any_node(segs[1], LOCK_NODES):
            return "lock"
        if len(segs) >= 3 and any_node(segs[1], COMM_NODES) and any_node(segs[2], RLST_NODES):
            return "lock"
    return None


def is_query_only(cmd: str) -> bool:
    """整条消息是否**纯查询**：每个 `;` 分段都必须是查询单元（问号后允许带参数）。

    为什么按"分段"而不是"整条"判：SCPI 里 `;` 分隔的是**同一条消息内的多个命令单元**，
    设备会逐个执行——实测 DG832 `:SOUR1:PHAS?;:SOUR1:PHAS 123` 的写单元真的生效
    （Keysight 手册明文：`TRIG:SOUR EXT;COUNT 10` 等价于两条命令）。只查首尾会让写命令
    从查询口溜进去；只允许单条单元又会把**多段回读**（`:CHANnel4:DISPlay?;:CHANnel4:SCALe?`）
    一起拒掉——那是合法且常用的用法（2026-09-15 曾这样过度收紧，用户报障后修回）。

    逐段判用同一条正则（一条命令单元 = `QUERY_UNIT_RE`），不解析助记符：
    写命令（头里无 `?`）、混合消息（`:OUTP1 ON;:OUTP1?`）、复位/锁定类一律拦。
    """
    units = [u for u in (p.strip() for p in (cmd or "").split(";"))]
    if not units or any(not u for u in units):
        return False
    return all(QUERY_UNIT_RE.match(u) for u in units)


def is_forbidden(cmd: str) -> bool:
    """黑名单判定（复位/存储覆写一律拦；远程锁定类**只拦写**）。

    语义：复位/存储覆写类一律 forbidden；远程锁定类只在**非纯查询**时拦——
    `SYST:REM?`、`:SYSTem:LOCKed?` 这类纯查询不改变锁定状态，保留用于状态诊断。
    判定按 `;` 分段做，且接受 SCPI 长短形式之间的任意前缀缩写。
    """
    kind = classify_forbidden(cmd)
    if kind == "reset":
        return True
    if kind == "lock":
        return not is_query_only(cmd)
    return False
