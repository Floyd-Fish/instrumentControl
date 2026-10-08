"""instrument_runtime — 仪器能力的单一事实源（Operation Registry）。

定位（见 docs/gpt_qa/2026-09-23-instrument-gateway-arch.md，方案 C 阶段 1）：
    本包把「一次可被调用的仪器操作」抽成一个显式对象 `Operation`，成为
    schema / 描述 / 安全分类 / 实现 的**唯一登记处**。将来 legacy MCP（68 个工具）、
    compact MCP（discover/search/describe/call/run）、CLI、文档都从这里派生，
    避免同一份知识在多处各写一遍后漂移。

分层纪律：
    * 本包**只依赖标准库**——不 import fastmcp / pyvisa / 设备库，保证可被任何
      frontend（MCP、CLI、代码运行时）复用，也保证离线可测。
    * schema 与 description **不在本包重写**：由 server.py 从 FastMCP 生成好的
      Tool 对象原样传入（见 `mcp_instruments/server.py` 的 `device_tool`）。
      这样「legacy 工具表逐字节不变」是构造保证，而不是靠人工同步。

阶段 1 的目标是**零行为变更**：68 个工具名、描述、schema 全部不动，只是让它们
额外登记进 registry，并用 verify_registry_parity.py 证明这一点。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Iterator, Mapping

__all__ = [
    "RISK_LEVELS",
    "Safety",
    "Operation",
    "Registry",
    "get_registry",
    "reset_registry",
    "register_operation",
    "resolve_canonical_id",
    "split_tool_name",
]

# 风险等级：从"只读"到"不可逆"，由低到高。compact profile 的 search 结果与
# policy broker 的放行判断都以它为依据。
RISK_LEVELS = ("read_only", "config", "output", "destructive")


@dataclass(frozen=True)
class Safety:
    """一次操作的安全属性（**数据**，不是散落在函数体里的 if）。

    requires_confirm 默认由 server.py 从函数签名推导（是否声明了 confirm 参数），
    因此"工具要求确认"这件事只有签名一个出处；catalog 若显式给出不同值，
    登记时会报错，避免两处打架。
    """

    risk: str
    requires_confirm: bool = False
    #: 该操作会把**任意 SCPI 字符串**送到设备（黑名单 _is_forbidden 的适用面）。
    raw_scpi: bool = False
    #: 执行后应做的验证，如 ("readback", "error_queue")。
    verify: tuple[str, ...] = ()
    note: str = ""

    def __post_init__(self) -> None:
        if self.risk not in RISK_LEVELS:
            raise ValueError(f"unknown risk {self.risk!r}; expected one of {RISK_LEVELS}")


@dataclass(frozen=True)
class Operation:
    """一个可被调用的仪器操作。"""

    #: 规范 id，形如 `sdg.set_wave`（由 tool_name 按首个 `_` 切分得来）。
    id: str
    #: legacy MCP 工具名，形如 `sdg_set_wave`（模型侧再套 mcp__<server>__ 前缀）。
    tool_name: str
    #: 设备族键（dg832 / sds / sdg / dmm / dho / mho / psu / ks3458a / instr / usb）。
    device: str
    #: 一句话摘要（取 docstring 首行），供 search 结果展示。
    summary: str
    #: 完整描述（= FastMCP 的 description，来自函数 docstring），legacy 面原样使用。
    description: str
    #: 入参 JSON Schema（= FastMCP 生成的 parameters，原样透传）。
    schema: Mapping[str, Any]
    safety: Safety
    #: 搜索用关键词（由名称与摘要派生，可加显式别名）。
    keywords: tuple[str, ...] = ()
    #: 声明式调用预算（原 @device_tool(budget_s=...) 的值，可为 None/可调用）。
    budget: Any = None
    #: 实现本体（server.py 里那个**同步原函数**，不是 async 包装）。
    fn: Callable[..., Any] | None = field(default=None, repr=False, compare=False)

    @property
    def parameters(self) -> tuple[str, ...]:
        """入参名（顺序即签名顺序），供 CLI/文档/代码运行时生成签名用。"""
        props = self.schema.get("properties") or {}
        return tuple(props.keys())


def split_tool_name(tool_name: str) -> tuple[str, str]:
    """把 `sdg_set_wave` 切成 `("sdg", "set_wave")`；无下划线时设备族为 `_`。"""
    if "_" not in tool_name:
        return "_", tool_name
    head, tail = tool_name.split("_", 1)
    return head, tail


def resolve_canonical_id(tool_name: str) -> str:
    """`sdg_set_wave` -> `sdg.set_wave`。"""
    head, tail = split_tool_name(tool_name)
    return f"{head}.{tail}"


def _summary_of(description: str) -> str:
    """摘要 = 描述的首个非空行（本仓 docstring 惯例：首行是概述）。"""
    for line in (description or "").splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def derive_keywords(tool_name: str, summary: str, extra: Iterable[str] = ()) -> tuple[str, ...]:
    """派生搜索关键词：名称分词 + 摘要里的 ASCII 词 + 显式别名。

    刻意保守——只做大小写归一的去重，不做词干化；68 个操作的规模用
    关键词/前缀排序已足够（见讨论存档 §2.3）。
    """
    import re

    words: list[str] = []
    head, tail = split_tool_name(tool_name)
    words.append(head)
    words.extend(p for p in tail.split("_") if p)
    words.extend(re.findall(r"[A-Za-z][A-Za-z0-9]{1,}", summary or ""))
    words.extend(extra)
    seen: dict[str, None] = {}
    for w in words:
        w = w.strip().lower()
        if len(w) >= 2:
            seen.setdefault(w, None)
    return tuple(seen)


class Registry:
    """操作登记处。登记顺序被保留（= legacy 工具注册顺序）。"""

    def __init__(self) -> None:
        self._ops: dict[str, Operation] = {}

    def register(self, op: Operation) -> Operation:
        if op.tool_name in self._ops:
            raise ValueError(f"duplicate operation tool_name: {op.tool_name}")
        if op.id in {o.id for o in self._ops.values()}:
            raise ValueError(f"duplicate operation id: {op.id}")
        self._ops[op.tool_name] = op
        return op

    def __len__(self) -> int:
        return len(self._ops)

    def __iter__(self) -> Iterator[Operation]:
        return iter(self._ops.values())

    def __contains__(self, tool_name: object) -> bool:
        return tool_name in self._ops

    def get(self, tool_name: str) -> Operation | None:
        return self._ops.get(tool_name)

    def require(self, tool_name: str) -> Operation:
        op = self._ops.get(tool_name)
        if op is None:
            raise KeyError(f"operation not registered: {tool_name}")
        return op

    def by_id(self, op_id: str) -> Operation | None:
        for op in self._ops.values():
            if op.id == op_id:
                return op
        return None

    def tool_names(self) -> tuple[str, ...]:
        return tuple(self._ops.keys())

    def by_device(self) -> dict[str, list[Operation]]:
        out: dict[str, list[Operation]] = {}
        for op in self._ops.values():
            out.setdefault(op.device, []).append(op)
        return out

    def snapshot(self) -> list[dict[str, Any]]:
        """JSON 可序列化快照（不含 fn），供留痕与 parity 校验。"""
        return [
            {
                "id": op.id,
                "tool_name": op.tool_name,
                "device": op.device,
                "summary": op.summary,
                "description": op.description,
                "schema": dict(op.schema),
                "safety": {
                    "risk": op.safety.risk,
                    "requires_confirm": op.safety.requires_confirm,
                    "raw_scpi": op.safety.raw_scpi,
                    "verify": list(op.safety.verify),
                    "note": op.safety.note,
                },
                "keywords": list(op.keywords),
            }
            for op in self._ops.values()
        ]


_REGISTRY: Registry | None = None


def get_registry() -> Registry:
    """取全局 registry（首次调用时创建）。"""
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = Registry()
    return _REGISTRY


def reset_registry() -> Registry:
    """丢弃并重建全局 registry（测试用；不用于生产路径）。"""
    global _REGISTRY
    _REGISTRY = Registry()
    return _REGISTRY


def register_operation(
    *,
    tool_name: str,
    description: str,
    schema: Mapping[str, Any],
    device: str,
    safety: Safety,
    budget: Any = None,
    fn: Callable[..., Any] | None = None,
    keywords: Iterable[str] = (),
    registry: Registry | None = None,
) -> Operation:
    """登记一个操作。id / summary / keywords 由入参派生，不重复传入。"""
    reg = registry if registry is not None else get_registry()
    op = Operation(
        id=resolve_canonical_id(tool_name),
        tool_name=tool_name,
        device=device,
        summary=_summary_of(description),
        description=description,
        schema=schema,
        safety=safety,
        keywords=derive_keywords(tool_name, _summary_of(description), keywords),
        budget=budget,
        fn=fn,
    )
    return reg.register(op)
