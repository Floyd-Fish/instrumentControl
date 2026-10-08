"""instrument_runtime — 仪器能力的单一事实源（Operation Registry）与护栏层。

见 `registry.py` 顶部说明与 `docs/gpt_qa/2026-09-23-instrument-gateway-arch.md`。

阶段 1：`registry` + `catalog`（能力登记与安全分类）。
阶段 2：`policy` / `audit` / `verify` / `broker`（黑名单、审计、回读验证、放行口
        与进程内设备锁）——这些**不依赖 MCP wrapper**，供 compact profile、CLI、
        代码运行时共用。依赖仅标准库，由 verify_broker_offline.py 断言。
"""
from . import audit, batch, broker, plan, policy, validate, verify
from .broker import (
    DEVICE_LOCK,
    ScpiDecision,
    acquire_device_lock,
    check_query_channel,
    check_write_channel,
    decide_scpi,
    release_device_lock,
)
from .catalog import (
    CONFIRM_TOOLS,
    DEVICE_BY_PREFIX,
    NOTES_BY_TOOL,
    RAW_SCPI_TOOLS,
    RISK_BY_TOOL,
    VERIFY_BY_TOOL,
    device_for_tool,
    safety_for_tool,
    unclassified,
)
from .plan import (
    PLAN_VERSION,
    SERVER_MAX_LEAF_STEPS,
    SERVER_MAX_NESTING_DEPTH,
    ValidatedPlan,
    validate_plan,
)
from .policy import classify_forbidden, is_forbidden, is_query_only
from .registry import (
    RISK_LEVELS,
    Operation,
    Registry,
    Safety,
    get_registry,
    register_operation,
    reset_registry,
    resolve_canonical_id,
    split_tool_name,
)

__all__ = [
    "CONFIRM_TOOLS",
    "DEVICE_BY_PREFIX",
    "DEVICE_LOCK",
    "NOTES_BY_TOOL",
    "PLAN_VERSION",
    "RAW_SCPI_TOOLS",
    "RISK_BY_TOOL",
    "RISK_LEVELS",
    "SERVER_MAX_LEAF_STEPS",
    "SERVER_MAX_NESTING_DEPTH",
    "VERIFY_BY_TOOL",
    "Operation",
    "Registry",
    "Safety",
    "ScpiDecision",
    "ValidatedPlan",
    "acquire_device_lock",
    "audit",
    "batch",
    "broker",
    "check_query_channel",
    "check_write_channel",
    "classify_forbidden",
    "decide_scpi",
    "device_for_tool",
    "get_registry",
    "is_forbidden",
    "is_query_only",
    "plan",
    "policy",
    "register_operation",
    "release_device_lock",
    "reset_registry",
    "resolve_canonical_id",
    "safety_for_tool",
    "split_tool_name",
    "unclassified",
    "validate",
    "validate_plan",
    "verify",
]
