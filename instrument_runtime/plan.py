"""Batch Plan DSL v1 —— 结构校验、词法作用域校验、规模统计（纯函数，零外部依赖）。

方案 C 阶段 4（见 docs/gpt_qa/2026-09-23-instrument-gateway-arch.md 的 Q4）。
设计契约来自那一轮的裁定，本模块是它的**可执行形态**：

    * 变量引用是**类型化节点** `{"$var": "name"}`，必须占据一个完整 JSON value；
      v1 **不支持**字符串模板（`"$freq"` 这类会被 operation schema 直接拒掉，
      且要处理转义/类型转换，得不偿失）。
    * **严格词法作用域**：`foreach` 的变量只在它自己的 body 内可见；`capture` 只在
      同一 lexical block 中**其后的 sibling** 可见；**foreach 内产生的 capture 不得
      逃逸到外层**（多轮迭代会产生多个同名值，没有合理的单值提升规则）。
    * **禁止 shadowing**：`foreach` 变量或 `capture` 与当前可见名重名一律拒绝。
    * 上述作用域问题必须**静态拒绝**（preflight 阶段），不能等执行到那里才发现。
    * `max_leaf_steps` 采用"**精确计数但不展开**"：只递归数基数，内存复杂度跟 AST
      大小相关，不跟展开后的叶子数相关；超限时**一次仪器调用都没发生**。
    * 服务器另有 hard limit，`plan` 不能靠写一个巨大的 `max_leaf_steps` 绕过资源限制。

验证次序（Q4 §四，刻意固定，不要再互换）：
    ① JSON parse（调用方负责）
    ② 结构校验  ③ plan_version
    ④ registry 查 op 是否存在
    ⑤ 词法作用域 / 变量绑定
    ⑥ max_nesting_depth
    ⑦ max_leaf_steps 精确计数
    ⑧ 服务器 hard limit
    ── 之后才提交为一个 executor job；每个 operation 在执行前才做变量解析与
       operation schema 校验（因为 `{"$var":...}` 解析成真实值之后才能校验类型）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, Mapping

__all__ = [
    "PLAN_VERSION",
    "SERVER_MAX_LEAF_STEPS",
    "SERVER_MAX_NESTING_DEPTH",
    "ON_ERROR_MODES",
    "PlanIssue",
    "ValidatedPlan",
    "VAR_KEY",
    "is_var_ref",
    "resolve_pointer",
    "validate_plan",
    "iter_leaf_ops",
]

PLAN_VERSION = 1

#: 服务器侧硬上限。plan 里的 max_leaf_steps 只是**更严**的自我约束，不能放宽这两个值。
SERVER_MAX_LEAF_STEPS = 500
SERVER_MAX_NESTING_DEPTH = 4

ON_ERROR_MODES = ("stop", "continue")

VAR_KEY = "$var"

_IDENT_RE = __import__("re").compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_OP_ID_RE = __import__("re").compile(r"^[A-Za-z][A-Za-z0-9_]*\.[A-Za-z][A-Za-z0-9_]*$")
_POINTER_RE = __import__("re").compile(r"^(?:/(?:[^~/]|~[01])*)+$")


@dataclass(frozen=True)
class PlanIssue:
    """一条校验问题。`path` 用 JSON Pointer 定位到 plan 里的位置，便于模型自纠。"""

    path: str
    code: str
    message: str

    def __str__(self) -> str:  # pragma: no cover - 展示用
        return f"{self.path}: [{self.code}] {self.message}"


@dataclass(frozen=True)
class ValidatedPlan:
    """通过 preflight 的 plan（仍是原始结构；执行期再解析变量）。"""

    plan_version: int
    steps: tuple
    on_error: str
    max_leaf_steps: int
    max_nesting_depth: int
    execution_deadline_s: float | None
    #: 精确叶子数（未展开计算所得）
    leaf_count: int
    #: 实际达到的最大嵌套深度
    depth: int
    #: 按执行次序展开的叶子 op 序列（仅 op id 与路径，不含参数）
    leaves: tuple = field(default=())


def is_var_ref(node: Any) -> bool:
    """`{"$var": "name"}` 判定（恰好一个键且值为合法标识符）。"""
    return (isinstance(node, dict) and len(node) == 1 and VAR_KEY in node
            and isinstance(node[VAR_KEY], str) and bool(_IDENT_RE.match(node[VAR_KEY])))


# ── JSON Pointer（RFC 6901）────────────────────────────────────────────────────

def resolve_pointer(doc: Any, pointer: str) -> tuple[bool, Any]:
    """按 RFC 6901 取值；返回 (found, value)。

    刻意**不做任何猜测**：取不到就是取不到，由调用方报 `capture_pointer_not_found`。
    转义规则：`~1` → `/`，`~0` → `~`（顺序不能反）。
    """
    if pointer == "":
        return True, doc
    if not _POINTER_RE.match(pointer):
        return False, None
    cur = doc
    for raw in pointer.split("/")[1:]:
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(cur, dict):
            if token not in cur:
                return False, None
            cur = cur[token]
        elif isinstance(cur, list):
            if not token.isdigit():
                return False, None
            idx = int(token)
            if idx >= len(cur):
                return False, None
            cur = cur[idx]
        else:
            return False, None
    return True, cur


# ── 结构校验 ──────────────────────────────────────────────────────────────────

def _issue(path: str, code: str, message: str) -> PlanIssue:
    return PlanIssue(path or "/", code, message)


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _check_literal(node: Any, path: str, issues: list[PlanIssue]) -> None:
    """literalValue：标量 / 数组 / 对象（对象不得含 `$var` 键，避免与引用混淆）。"""
    if isinstance(node, (str, bool)) or node is None or _is_number(node):
        return
    if isinstance(node, list):
        for i, item in enumerate(node):
            _check_literal(item, f"{path}/{i}", issues)
        return
    if isinstance(node, dict):
        if VAR_KEY in node:
            issues.append(_issue(path, "var_in_literal",
                                 f"字面量对象里不允许出现 {VAR_KEY} 键（会与变量引用混淆）"))
            return
        for k, v in node.items():
            _check_literal(v, f"{path}/{k}", issues)
        return
    issues.append(_issue(path, "bad_literal", f"不是合法的字面量：{type(node).__name__}"))


def _check_runtime_value(node: Any, path: str, issues: list[PlanIssue]) -> None:
    """runtimeValue：字面量，或一个完整的 `{"$var": ...}` 节点。"""
    if isinstance(node, dict) and VAR_KEY in node:
        if not is_var_ref(node):
            issues.append(_issue(path, "bad_var_ref",
                                 f"{VAR_KEY} 引用必须是 {{'$var': '<标识符>'}} 且只有这一个键"))
        return
    _check_literal(node, path, issues)


def _check_args(args: Any, path: str, issues: list[PlanIssue]) -> None:
    if not isinstance(args, dict):
        issues.append(_issue(path, "bad_args", "args 必须是对象"))
        return
    if VAR_KEY in args:
        issues.append(_issue(path, "var_in_args", f"args 本身不能是 {VAR_KEY} 引用"))
    for k, v in args.items():
        _check_runtime_value(v, f"{path}/{k}", issues)


def _check_capture(cap: Any, path: str, issues: list[PlanIssue]) -> None:
    if not isinstance(cap, dict):
        issues.append(_issue(path, "bad_capture", "capture 必须是对象"))
        return
    extra = set(cap) - {"var", "source", "pointer"}
    if extra:
        issues.append(_issue(path, "bad_capture", f"capture 含未知键：{sorted(extra)}"))
    var = cap.get("var")
    if not isinstance(var, str) or not _IDENT_RE.match(var):
        issues.append(_issue(f"{path}/var", "bad_identifier", f"capture.var 不是合法标识符：{var!r}"))
    if cap.get("source") != "result":
        issues.append(_issue(f"{path}/source", "bad_capture_source",
                             "capture.source 目前只支持 'result'"))
    if "pointer" in cap:
        p = cap["pointer"]
        if not isinstance(p, str) or not _POINTER_RE.match(p):
            issues.append(_issue(f"{path}/pointer", "bad_pointer",
                                 f"capture.pointer 不是合法的 RFC 6901 JSON Pointer：{p!r}"))


def _check_steps(steps: Any, path: str, depth: int, max_depth: int,
                 issues: list[PlanIssue]) -> None:
    if not isinstance(steps, list) or not steps:
        issues.append(_issue(path, "bad_steps", "steps 必须是非空数组"))
        return
    if depth > max_depth:
        issues.append(_issue(path, "too_deep",
                             f"嵌套深度 {depth} 超过 max_nesting_depth={max_depth}"))
        return
    for i, step in enumerate(steps):
        sp = f"{path}/{i}"
        if not isinstance(step, dict):
            issues.append(_issue(sp, "bad_step", "step 必须是对象"))
            continue
        if "foreach" in step:
            extra = set(step) - {"foreach", "steps"}
            if extra:
                issues.append(_issue(sp, "bad_step", f"foreach step 含未知键：{sorted(extra)}"))
            fs = step.get("foreach")
            if not isinstance(fs, dict):
                issues.append(_issue(f"{sp}/foreach", "bad_foreach", "foreach 必须是对象"))
            else:
                fextra = set(fs) - {"var", "values"}
                if fextra:
                    issues.append(_issue(f"{sp}/foreach", "bad_foreach",
                                         f"foreach 含未知键：{sorted(fextra)}"))
                fvar = fs.get("var")
                if not isinstance(fvar, str) or not _IDENT_RE.match(fvar):
                    issues.append(_issue(f"{sp}/foreach/var", "bad_identifier",
                                         f"foreach.var 不是合法标识符：{fvar!r}"))
                vals = fs.get("values")
                if not isinstance(vals, list) or not vals:
                    issues.append(_issue(f"{sp}/foreach/values", "bad_foreach_values",
                                         "foreach.values 必须是非空数组（只接受字面量）"))
                else:
                    _check_literal(vals, f"{sp}/foreach/values", issues)
            _check_steps(step.get("steps"), f"{sp}/steps", depth + 1, max_depth, issues)
        elif "op" in step:
            extra = set(step) - {"op", "args", "capture"}
            if extra:
                issues.append(_issue(sp, "bad_step", f"operation step 含未知键：{sorted(extra)}"))
            op = step.get("op")
            if not isinstance(op, str) or not _OP_ID_RE.match(op):
                issues.append(_issue(f"{sp}/op", "bad_op_id",
                                     f"op 必须是 '<设备族>.<操作>' 形式：{op!r}"))
            if "args" in step:
                _check_args(step["args"], f"{sp}/args", issues)
            if "capture" in step:
                _check_capture(step["capture"], f"{sp}/capture", issues)
        else:
            issues.append(_issue(sp, "bad_step", "step 必须是 operation（含 op）或 foreach"))


# ── 规模统计（精确计数、不展开）────────────────────────────────────────────────

def _count_leaves(steps: list, limit: int) -> int:
    """精确计数；一旦超过 limit 立即返回 limit+1（不继续算，也不展开结构）。

    这与 Q4 给的 `count_steps` 同构：嵌套 foreach 用乘法而非展开。
    """
    total = 0
    for step in steps:
        if not isinstance(step, dict):
            continue
        if "op" in step:
            total += 1
        elif "foreach" in step:
            fs = step.get("foreach") or {}
            vals = fs.get("values") if isinstance(fs, dict) else None
            n = len(vals) if isinstance(vals, list) else 0
            inner = _count_leaves(step.get("steps") or [], limit)
            product = n * inner
            if product > limit or total + product > limit:
                return limit + 1
            total += product
        if total > limit:
            return limit + 1
    return total


def iter_leaf_ops(steps: Iterable, base_path: str = "") -> Iterator[tuple[str, dict]]:
    """按执行次序产出 (路径, operation step)；foreach **不展开**，只展开其 body 一次。

    用途：preflight 的作用域校验（每个 leaf 只检查一次即可，迭代次数不影响绑定关系）。
    """
    for i, step in enumerate(steps):
        p = f"{base_path}/steps/{i}"
        if not isinstance(step, dict):
            continue
        if "op" in step:
            yield p, step
        elif "foreach" in step:
            yield from iter_leaf_ops(step.get("steps") or [], p)


# ── 词法作用域校验 ────────────────────────────────────────────────────────────

def _walk_scope(steps: Any, scope: tuple[frozenset, ...], path: str,
                issues: list[PlanIssue]) -> None:
    """遍历 steps，维护作用域栈；只做静态检查，不执行。

    作用域栈：外层在前。可见名 = 栈内所有集合的并集。
    每个 lexical block（root 或某个 foreach body）有**自己的** local 集合，
    `capture` 写入当前 block 的 local —— 于是 foreach 内的 capture 天然随该 block
    结束而消失，**不可能逃逸到外层**（这正是 Q4 的裁定，且是结构性保证而非靠检查）。
    """
    if not isinstance(steps, list):
        return
    local: set[str] = set()          # 当前 block 内新声明的名字

    def visible(extra: frozenset) -> set[str]:
        out: set[str] = set()
        for s in scope:
            out |= s
        out |= extra
        return out

    for i, step in enumerate(steps):
        sp = f"{path}/steps/{i}"
        if not isinstance(step, dict):
            continue
        if "op" in step:
            # 校验该 leaf 的所有 $var 引用都能解析
            for ref_path, name in _collect_var_refs(step.get("args"), f"{sp}/args"):
                if name not in visible(frozenset(local)):
                    issues.append(_issue(ref_path, "undefined_variable",
                                         f"变量 {name!r} 在此处不可见（严格词法作用域；"
                                         f"foreach 内 capture 的值不能带到外层）"))
            cap = step.get("capture")
            if isinstance(cap, dict) and isinstance(cap.get("var"), str):
                cv = cap["var"]
                if cv in local:
                    issues.append(_issue(f"{sp}/capture/var", "duplicate_variable",
                                         f"同一作用域内重复声明 {cv!r}（v1 禁止 shadowing）"))
                elif cv in visible(frozenset()):
                    issues.append(_issue(f"{sp}/capture/var", "duplicate_variable",
                                         f"capture 变量 {cv!r} 与可见的外层变量重名"
                                         f"（v1 禁止 shadowing）"))
                else:
                    local.add(cv)
        elif "foreach" in step:
            fs = step.get("foreach") or {}
            fvar = fs.get("var") if isinstance(fs, dict) else None
            if isinstance(fvar, str) and _IDENT_RE.match(fvar):
                if fvar in local:
                    issues.append(_issue(f"{sp}/foreach/var", "duplicate_variable",
                                         f"同一作用域内重复声明 {fvar!r}（v1 禁止 shadowing）"))
                elif fvar in visible(frozenset()):
                    issues.append(_issue(f"{sp}/foreach/var", "duplicate_variable",
                                         f"foreach 变量 {fvar!r} 与可见的外层变量重名"))
            # body 的新作用域：外层可见 ∪ {foreach var}
            inner_scope = scope + (frozenset(local),)
            if isinstance(fvar, str):
                inner_scope = inner_scope + (frozenset({fvar}),)
            _walk_scope(step.get("steps"), inner_scope, sp, issues)


def _collect_var_refs(node: Any, path: str) -> Iterator[tuple[str, str]]:
    """递归收集 runtimeValue 里的 `$var` 引用，产出 (路径, 变量名)。"""
    if is_var_ref(node):
        yield path, node[VAR_KEY]
        return
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _collect_var_refs(v, f"{path}/{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _collect_var_refs(v, f"{path}/{i}")


# ── 主入口 ────────────────────────────────────────────────────────────────────

def validate_plan(raw: Any, *, op_exists=None) -> tuple[ValidatedPlan | None, list[PlanIssue]]:
    """按 Q4 §四 的次序做完整 preflight。

    `op_exists(op_id) -> bool` 用于 registry 查表（缺省则跳过第 ④ 步，便于离线单测）。
    返回 (plan, issues)；issues 非空时 plan 为 None，且**一次仪器调用都不会发生**。
    """
    issues: list[PlanIssue] = []

    # ② 结构
    if not isinstance(raw, dict):
        return None, [_issue("/", "bad_plan", "plan 必须是 JSON 对象")]
    allowed = {"plan_version", "steps", "on_error", "execution_deadline_s",
               "max_leaf_steps", "max_nesting_depth"}
    extra = set(raw) - allowed
    if extra:
        issues.append(_issue("/", "unknown_key", f"plan 含未知键：{sorted(extra)}"))
    for key in ("plan_version", "steps", "on_error", "max_leaf_steps", "max_nesting_depth"):
        if key not in raw:
            issues.append(_issue("/", "missing_key", f"缺少必填键 {key!r}"))

    # ③ plan_version
    pv = raw.get("plan_version")
    if pv != PLAN_VERSION:
        issues.append(_issue("/plan_version", "bad_plan_version",
                             f"plan_version 必须是 {PLAN_VERSION}，收到 {pv!r}"))
    # on_error
    on_error = raw.get("on_error")
    if on_error not in ON_ERROR_MODES:
        issues.append(_issue("/on_error", "bad_on_error",
                             f"on_error 必须是 {list(ON_ERROR_MODES)} 之一，收到 {on_error!r}"))
    # 规模上限（先做大致的类型检查，再做精确计数）
    mls = raw.get("max_leaf_steps")
    if not isinstance(mls, int) or isinstance(mls, bool) or mls < 1:
        issues.append(_issue("/max_leaf_steps", "bad_max_leaf_steps",
                             "max_leaf_steps 必须是 ≥1 的整数"))
        mls = None
    mnd = raw.get("max_nesting_depth")
    if not isinstance(mnd, int) or isinstance(mnd, bool) or mnd < 0:
        issues.append(_issue("/max_nesting_depth", "bad_max_nesting_depth",
                             "max_nesting_depth 必须是 ≥0 的整数"))
        mnd = None
    dl = raw.get("execution_deadline_s")
    if dl is not None and (not _is_number(dl) or dl <= 0):
        issues.append(_issue("/execution_deadline_s", "bad_deadline",
                             "execution_deadline_s 必须是 >0 的数"))
    if issues:
        return None, issues

    max_depth = int(mnd)
    # 深度从 0 起算：**只数 foreach 层数**。max_nesting_depth=0 表示"不许用 foreach"。
    _check_steps(raw["steps"], "", 0, max_depth, issues)
    if issues:
        return None, issues

    # ④ registry 查 op
    if op_exists is not None:
        for p, step in iter_leaf_ops(raw["steps"]):
            if not op_exists(step["op"]):
                issues.append(_issue(f"{p}/op", "unknown_operation",
                                     f"未登记的操作 {step['op']!r}；用 instr_search 查找"))
        if issues:
            return None, issues

    # ⑤ 词法作用域
    _walk_scope(raw["steps"], tuple(), "", issues)
    # ⑥ 嵌套深度（_check_steps 已按 max_depth 拦过；这里记录实际深度）
    depth = _measure_depth(raw["steps"])
    # ⑦⑧ 精确计数 + 服务器硬上限
    effective = min(int(mls), SERVER_MAX_LEAF_STEPS)
    count = _count_leaves(raw["steps"], effective)
    if count > effective:
        issues.append(_issue("/max_leaf_steps", "too_many_leaf_steps",
                             f"展开后的叶子数超过上限 {effective}"
                             f"（plan 声明 {int(mls)}，服务器硬上限 {SERVER_MAX_LEAF_STEPS}）"))
    if depth > SERVER_MAX_NESTING_DEPTH:
        issues.append(_issue("/", "too_deep_server_limit",
                             f"嵌套深度 {depth} 超过服务器硬上限 {SERVER_MAX_NESTING_DEPTH}"))
    if issues:
        return None, issues

    return ValidatedPlan(
        plan_version=PLAN_VERSION,
        steps=tuple(raw["steps"]),
        on_error=on_error,
        max_leaf_steps=int(mls),
        max_nesting_depth=max_depth,
        execution_deadline_s=(float(dl) if dl is not None else None),
        leaf_count=count,
        depth=depth,
    ), []


def _measure_depth(steps: Any, depth: int = 0) -> int:
    """实际最大嵌套深度（只数 foreach 层数；root 为 0）。"""
    if not isinstance(steps, list):
        return depth
    best = depth
    for step in steps:
        if isinstance(step, dict) and "foreach" in step:
            best = max(best, _measure_depth(step.get("steps"), depth + 1))
    return best
