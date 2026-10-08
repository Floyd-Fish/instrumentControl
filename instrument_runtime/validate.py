"""入参校验——按操作自己的 JSON Schema 检查 `instr_call` 收到的参数。

为什么需要它（见 docs/gpt_qa/2026-09-23-instrument-gateway-arch.md §2.1）：
改成网关式调用后，模型不再看到 68 份静态 schema，若服务端也不校验，参数名写错、
单位搞混、必填缺失就只能等设备层抛异常——错误信息离原因很远，小模型尤其容易
在重试里打转。所以"静态 schema 从上下文里省掉"必须配套"服务端仍按同一份 schema
严格校验"，损失才只是可见性，不是正确性。

覆盖范围**刻意贴合本仓实际**（2026-09-23 对 68 个 schema 做过统计）：只用到
`type`(object/integer/boolean/string/number)、`anyOf`（可选参数为 `[{type:X},
{type:null}]`）、`required` 三种构造，**没有 enum、数组与嵌套对象**。因此这里不实现
通用 JSON Schema——过度实现的部分既没人验证，又会掩盖"schema 形态变了"这件事。
`schema_constructs()` 用于在构造超出预期时报警（见 verify_compact_profile.py）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

__all__ = [
    "ValidationIssue",
    "SUPPORTED_TYPES",
    "schema_constructs",
    "validate_args",
    "validate_for_operation",
    "format_issues",
]

SUPPORTED_TYPES = ("object", "integer", "boolean", "string", "number")


@dataclass(frozen=True)
class ValidationIssue:
    param: str
    message: str

    def __str__(self) -> str:  # pragma: no cover - 展示用
        return f"{self.param}: {self.message}"


def _type_ok(value: Any, expected: str) -> bool:
    """单类型检查。注意 Python 的 bool 是 int 的子类，必须显式排除。"""
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        if isinstance(value, bool):
            return False
        if isinstance(value, int):
            return True
        return isinstance(value, float) and value.is_integer()
    if expected == "number":
        return not isinstance(value, bool) and isinstance(value, (int, float))
    if expected == "null":
        return value is None
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    return True  # 未知类型不拦（宁可放过，也不要因 schema 演进误拒合法调用）


def _accepts(spec: Mapping[str, Any], value: Any) -> bool:
    """按一个属性规格判断取值是否可接受（含 anyOf / 无类型声明）。"""
    if "anyOf" in spec:
        return any(_accepts(alt, value) for alt in spec["anyOf"] if isinstance(alt, dict))
    if "enum" in spec:
        return value in spec["enum"]
    expected = spec.get("type")
    if expected is None:
        return True  # 未声明类型 = 不限制
    if isinstance(expected, list):
        return any(_type_ok(value, t) for t in expected)
    return _type_ok(value, expected)


def schema_constructs(schema: Mapping[str, Any]) -> set[str]:
    """列出 schema 实际用到的 JSON Schema **关键字**（供校验器覆盖面审计）。

    注意：`properties` 下面的键是**参数名**（`ch`、`amp`、`action` …），不是关键字。
    必须跳过，否则审计结果会被几十个参数名淹没而看不出真正的构造
    （2026-09-23 首次跑就踩了：报出 100 多个"未知构造"，全是参数名）。
    """
    found: set[str] = set()

    def walk(node: Any, in_properties: bool = False) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if not in_properties:
                    found.add(k)
                # 只有 properties 的直接子键是"名字"；它们的值仍是 schema，要继续走。
                walk(v, in_properties=(k == "properties"))
        elif isinstance(node, list):
            for x in node:
                walk(x, in_properties=False)

    walk(schema)
    return found


def validate_args(schema: Mapping[str, Any], args: Any) -> list[ValidationIssue]:
    """按 schema 校验 args（预期是 dict）。返回问题列表，空列表 = 通过。"""
    if not isinstance(args, dict):
        return [ValidationIssue("<args>", f"必须是对象（JSON object），收到 {type(args).__name__}")]

    props: Mapping[str, Any] = schema.get("properties") or {}
    required: Iterable[str] = schema.get("required") or []
    issues: list[ValidationIssue] = []

    for key in required:
        if key not in args:
            issues.append(ValidationIssue(key, "缺少必填参数"))
    for key, value in args.items():
        if key not in props:
            known = ", ".join(sorted(props)) or "(无)"
            issues.append(ValidationIssue(key, f"未知参数；本操作接受的参数：{known}"))
            continue
        spec = props[key]
        if value is None and _accepts(spec, None):
            continue  # 显式传 null 且 schema 允许 = 合法
        if not _accepts(spec, value):
            want = _describe_type(spec)
            issues.append(ValidationIssue(
                key, f"类型不符：期望 {want}，收到 {type(value).__name__}（{value!r}）"))
    return issues


def _describe_type(spec: Mapping[str, Any]) -> str:
    if "anyOf" in spec:
        return " 或 ".join(_describe_type(a) for a in spec["anyOf"] if isinstance(a, dict))
    if "enum" in spec:
        return "|".join(str(e) for e in spec["enum"])
    t = spec.get("type")
    if isinstance(t, list):
        return " 或 ".join(str(x) for x in t)
    return str(t if t is not None else "任意")


def format_issues(issues: Iterable[ValidationIssue]) -> str:
    return "; ".join(str(i) for i in issues)


def validate_for_operation(op: Any, args: Any) -> list[ValidationIssue]:
    """按某个操作的 schema 校验入参。

    `op` 只要求有 `.schema` 属性（鸭子类型）——这样本模块不必 import registry，
    保持 validate ↔ registry 无环。
    """
    return validate_args(getattr(op, "schema", None) or {}, args)
