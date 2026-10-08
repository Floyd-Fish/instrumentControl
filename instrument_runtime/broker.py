"""Policy Broker——agent 控制路径的**唯一放行口**。

方案 C 阶段 2（见 docs/gpt_qa/2026-09-23-instrument-gateway-arch.md §3）：把
「黑名单 / 审计 / 锁 / 回读验证」四件事收进一个**不依赖 MCP wrapper** 的层。
意义在于：compact profile、CLI、代码运行时将来都要放行仪器操作，如果各自
重新实现一遍判据，早晚会出现"某个入口忘了拦"——那是安全洞，不是风格问题。

分层约定：
    * 本模块只依赖标准库 + `instrument_runtime` 内部模块。
    * **不 import fastmcp / mcp / pyvisa**——由 verify_broker_offline.py 断言，
      防止将来有人图省事在这里 import 设备库，把 runtime 层重新绑死到 MCP 上。
    * 设备 I/O 的执行器（预算/超时/线程封闭）**不在本模块**：它需要真实仪器才能
      验证，留在 server.py 由后续阶段迁移。

关于 `confirm`：本阶段**不改动** `confirm=True` 的现有语义（用户 2026-09-23 决定）。
它在讨论存档里被指出"模型可自填、不构成强授权"，升级为 arm/capability 是独立议题。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass

from .audit import audit_scpi
from .policy import classify_forbidden, is_forbidden, is_query_only
from .verify import ERR_CLEAN_RE, drain_errors, pair_readback

__all__ = [
    "DEVICE_LOCK",
    "ScpiDecision",
    "acquire_device_lock",
    "release_device_lock",
    "decide_scpi",
    "check_query_channel",
    "check_write_channel",
    "audit_scpi",
    "pair_readback",
    "drain_errors",
    "ERR_CLEAN_RE",
]

#: 进程内设备锁（原 `server._DEVICE_LOCK`，**同一个对象**）。
#:
#: 语义保持原样：单 worker 执行器下正常情况下没有竞争者，这把锁是第二道防线——
#: 防止将来有代码绕过执行器直接碰 VISA。真要等满 timeout，恰恰说明存在这样的
#: 旁路或异常路径长期占锁，是有价值的告警信号，所以**不改成可重入锁**。
DEVICE_LOCK = threading.Lock()


def acquire_device_lock(wait_s: float) -> bool:
    """尝试获取设备锁；返回是否成功（失败即调用方应回 device_busy）。"""
    return DEVICE_LOCK.acquire(timeout=max(0.0, wait_s))


def release_device_lock() -> None:
    """释放设备锁。"""
    DEVICE_LOCK.release()


@dataclass(frozen=True)
class ScpiDecision:
    """一条原始 SCPI 消息的放行判定结果。

    `reason` 是**机器可读**的类别，不是给用户看的文案——各调用点的错误文案与
    error_type 各不相同（instr_query / instr_write / dg_query 各有措辞），
    所以这里只给判定，文案仍由调用点负责，从而保证迁移后返回体逐字不变。
    """

    allowed: bool
    #: "not_query" | "forbidden_reset" | "forbidden_lock" | "empty" | None
    reason: str | None = None
    #: 黑名单类别（policy.classify_forbidden 的结果），None 表示未命中。
    kind: str | None = None

    def __bool__(self) -> bool:  # 允许 `if decision:`
        return self.allowed


def decide_scpi(cmd: str, *, require_query: bool) -> ScpiDecision:
    """判定一条原始 SCPI 消息可否下发。

    require_query=True  —— 纯查询通道（instr_query / dg_query）：整条必须是纯查询，
                           且不得命中黑名单。
    require_query=False —— 原始写通道（instr_write）：只做黑名单判定（复位/存储覆写
                           一律拦；锁定类只在非纯查询时拦——由 is_forbidden 内部处理）。

    判据本身全部来自 `policy`，本函数只负责**组合顺序**；顺序与原三个调用点一致，
    因此结果等价（verify_broker_offline.py 用参考表达式逐条比对证明）。
    """
    kind = classify_forbidden(cmd)
    if require_query:
        if is_forbidden(cmd) or not is_query_only(cmd):
            if kind == "reset":
                return ScpiDecision(False, "forbidden_reset", kind)
            if kind == "lock":
                return ScpiDecision(False, "forbidden_lock", kind)
            return ScpiDecision(False, "not_query", kind)
        return ScpiDecision(True, None, kind)
    if is_forbidden(cmd):
        return ScpiDecision(False, f"forbidden_{kind or 'reset'}", kind)
    return ScpiDecision(True, None, kind)


def check_query_channel(cmd: str) -> ScpiDecision:
    """纯查询通道的放行判定（语义糖，避免调用点记错 require_query 的取值）。"""
    return decide_scpi(cmd, require_query=True)


def check_write_channel(cmd: str) -> ScpiDecision:
    """原始写通道的黑名单判定（语义糖）。注意：**不含 confirm 判定**——
    confirm 是调用点的授权语义，不属于 SCPI 判据。"""
    return decide_scpi(cmd, require_query=False)
