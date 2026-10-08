"""Batch 解释器 —— 在**一个 executor job 内**执行整份 plan。

方案 C 阶段 4（设计契约见 docs/gpt_qa/2026-09-23-instrument-gateway-arch.md 的 Q3/Q4）。
本模块只做"解释执行"，不碰 MCP、不碰执行器：`invoke` 由 server.py 注入。

三条必须守住的语义（Q3/Q4 裁定，改这里之前先读）：

1. **整批是一个 executor job**。`instr_batch` 提交**一次** `executor.run(...)`，BUSY 在
   整批期间保持 —— 于是进程内其它设备调用拿到 `device_busy` 而**不会插进扫频中间**。
   这保证的是"进程内 non-interleaving"，**不是**事务原子性：没有回滚，跨进程也不保证互斥。
   若改成"每个叶子各提交一个 job"，BUSY 会在步与步之间释放，别的调用可以插进来改频率，
   扫频结果就被污染了 —— 那是实打实的正确性问题。
2. **batch 自己不持 `_DEVICE_LOCK`**。每个 operation 内部照常经它的 `_call` 获取普通
   `threading.Lock`（无嵌套 → 无重入死锁）。所以 `invoke` 必须是**叶子操作自己的实现**，
   **绝不能**再走一次 executor 或走公开的 `instr_call`（那会形成 executor 自调用）。
3. **deadline 是协作式的**：在每个叶子操作**开始前**检查，到期就不再启动新叶子；
   已经启动的那个**允许自然结束**并记录其结果。所以上界是"最多再完成一个 canonical
   operation"——**不能**承诺"最多再发一条 SCPI"（一个 operation 内部可能发多条）。
   本仓底层 native 调用无法安全中断，声称"已取消"会是错误事实。

失败与部分结果：`on_error=stop` 时**已产生的 leaf_results / capture_log 全部返回**，
不丢 —— 仪器侧的副作用已经发生，隐瞒会让调用方失去判断设备当前状态所需的信息。
capture 以**事件日志**返回（不是扁平 dict）：foreach 里同一个变量名在多轮迭代中合法存在，
扁平 dict 会有作用域歧义。
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from .plan import VAR_KEY, ValidatedPlan, is_var_ref, resolve_pointer
from .validate import format_issues, validate_for_operation

__all__ = ["run_plan", "expand_leaves", "resolve_args"]

#: 会话锁告警的稳定特征串（common/session_lock.py 生成）——用于把**跨进程争用**单独聚合。
_CONTENTION_MARK = "另一个进程"
_CONTENTION_SAME_ADDR = "同一地址"


def expand_leaves(steps, path: str = "", env: Mapping[str, Any] | None = None
                  ) -> Iterator[tuple[str, dict, dict]]:
    """按执行次序展开叶子操作；产出 (路径, operation step, 变量环境)。

    foreach 在这里展开（数量已由 preflight 用"精确计数不展开"卡住上限）。
    路径形如 `/steps/0/iterations/1/steps/1`，与 capture_log 的 path 一致。
    """
    env = dict(env or {})
    for i, step in enumerate(steps or []):
        sp = f"{path}/steps/{i}"
        if not isinstance(step, dict):
            continue
        if "op" in step:
            yield sp, step, env
        elif "foreach" in step:
            fs = step.get("foreach") or {}
            var = fs.get("var")
            for k, val in enumerate(fs.get("values") or []):
                child = dict(env)
                if isinstance(var, str):
                    child[var] = val
                yield from expand_leaves(step.get("steps"), f"{sp}/iterations/{k}", child)


def resolve_args(node: Any, env: Mapping[str, Any], path: str = "/args"
                 ) -> tuple[Any, list[str]]:
    """把 args 里的 `{"$var": ...}` 解析成真实值；返回 (解析结果, 错误列表)。

    必须在 operation schema 校验**之前**做：`{"$var":"freq"}` 在 plan 层合法，
    解析成 `1000` 之后才能判断它是否满足 operation 的参数类型（Q4 §四 的刻意保留点）。
    """
    errs: list[str] = []
    if is_var_ref(node):
        name = node[VAR_KEY]
        if name not in env:
            errs.append(f"{path}: 变量 {name!r} 在执行期不可见")
            return None, errs
        return env[name], errs
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            rv, e = resolve_args(v, env, f"{path}/{k}")
            errs.extend(e)
            out[k] = rv
        return out, errs
    if isinstance(node, list):
        out = []
        for i, v in enumerate(node):
            rv, e = resolve_args(v, env, f"{path}/{i}")
            errs.extend(e)
            out.append(rv)
        return out, errs
    return node, errs


def _classify_warnings(warnings: list[dict]) -> list[dict]:
    """把跨进程争用类告警单独聚合出来（Q3 的要求）。

    动机：跨进程竞争时可能出现**合法但属于另一请求的响应**，那意味着该次测量的
    完整性无法证明。这类告警绝不能淹没在某个中间 step 的返回值里，必须提升到 run 级别。
    注意**不因此把 ok 改成 false** —— 既有口径是"只告警、由调用方裁决"。
    """
    same_addr: list[int] = []
    other_iface: list[int] = []
    for i, w in enumerate(warnings):
        msg = w.get("message") or ""
        if _CONTENTION_MARK not in msg:
            continue
        if _CONTENTION_SAME_ADDR in msg:
            same_addr.append(i)
        else:
            other_iface.append(i)
    out: list[dict] = []
    if same_addr:
        out.append({"code": "external_session_contention",
                    "detail": "同一地址被另一进程占用（并发会响应串台，测量完整性无法证明）",
                    "steps": same_addr})
    if other_iface:
        out.append({"code": "external_session_other_interface",
                    "detail": "同一台仪器的另一接口被占用（跨接口并发是否安全尚未验证）",
                    "steps": other_iface})
    return out


def run_plan(plan: ValidatedPlan, *, invoke: Callable[[str, dict], str],
             registry, artifact_dir: str | Path | None = None,
             max_leaf_steps: int | None = None,
             clock: Callable[[], float] = time.monotonic) -> dict:
    """执行整份 plan，返回可 JSON 序列化的 run 摘要。

    invoke(op_id, args) 由 server.py 注入：它**已经处于 executor worker 内**，
    直接调用叶子操作的实现（见模块顶部第 2 条）。
    """
    started = clock()
    t0 = datetime.now()
    run_id = t0.strftime("%Y%m%d_%H%M%S")
    deadline_s = plan.execution_deadline_s
    budget = plan.leaf_count if max_leaf_steps is None else min(plan.leaf_count, max_leaf_steps)

    leaf_results: list[dict] = []
    capture_log: list[dict] = []
    warnings: list[dict] = []
    success = 0
    failed_step: dict | None = None
    status = "completed"

    for path, step, env in expand_leaves(plan.steps):
        # ── 3. deadline 只在**叶子开始前**检查；已启动的叶子允许自然结束 ──
        if deadline_s is not None and (clock() - started) >= deadline_s:
            status = "deadline_reached"
            break
        if len(leaf_results) >= budget:
            status = "max_leaf_steps_reached"
            break

        op_id = step["op"]
        op = registry.by_id(op_id) or registry.get(op_id)
        if op is None:
            # preflight 已查过 registry；走到这里说明注册表在运行期变了（HMR/重载）
            leaf_results.append({"path": path, "op": op_id, "ok": False,
                                 "error_type": "unknown_operation",
                                 "error": "操作在运行期已不在注册表中"})
            failed_step = {"path": path, "op": op_id, "error_type": "unknown_operation"}
            if plan.on_error == "stop":
                status = "stopped_on_error"
                break
            continue

        # ── b. 变量解析 ──
        args, rerrs = resolve_args(step.get("args") or {}, env, f"{path}/args")
        if rerrs:
            leaf_results.append({"path": path, "op": op_id, "ok": False,
                                 "error_type": "unresolved_variable",
                                 "error": "; ".join(rerrs)})
            failed_step = {"path": path, "op": op_id, "error_type": "unresolved_variable"}
            if plan.on_error == "stop":
                status = "stopped_on_error"
                break
            continue

        # ── c. operation schema 校验（解析之后做，见 resolve_args 注释）──
        issues = validate_for_operation(op, args)
        if issues:
            msg = format_issues(issues)
            leaf_results.append({"path": path, "op": op_id, "ok": False,
                                 "error_type": "param_validation", "error": msg})
            failed_step = {"path": path, "op": op_id, "error_type": "param_validation"}
            if plan.on_error == "stop":
                status = "stopped_on_error"
                break
            continue

        # ── d. 调用 canonical operation（同 legacy / instr_call 的实现）──
        t_leaf = clock()
        try:
            raw = invoke(op.tool_name, dict(args))
        except Exception as e:                                   # noqa: BLE001
            leaf_results.append({"path": path, "op": op_id, "ok": False,
                                 "error_type": "invoke_exception",
                                 "error": f"{type(e).__name__}: {e}"})
            failed_step = {"path": path, "op": op_id, "error_type": "invoke_exception"}
            if plan.on_error == "stop":
                status = "stopped_on_error"
                break
            continue
        elapsed_ms = int((clock() - t_leaf) * 1000)

        # ── e. 解析返回体（operation 统一返回 JSON 字符串）──
        try:
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise ValueError("返回体不是 JSON 对象")
        except Exception:                                        # noqa: BLE001
            leaf_results.append({"path": path, "op": op_id, "ok": False,
                                 "error_type": "invalid_operation_response",
                                 "error": "operation 返回体不是合法 JSON 对象",
                                 "raw_head": str(raw)[:200]})
            failed_step = {"path": path, "op": op_id, "error_type": "invalid_operation_response"}
            if plan.on_error == "stop":
                status = "stopped_on_error"
                break
            continue

        # 收集返回体自带的告警（含跨进程会话争用），提升到 run 级别
        for w in (parsed.get("warnings") or []):
            warnings.append({"step": path, "op": op_id, "message": str(w)})

        ok = bool(parsed.get("ok"))
        rec = {"path": path, "op": op_id, "ok": ok, "elapsed_ms": elapsed_ms}
        if ok:
            success += 1
            rec["result"] = parsed.get("result")
            if parsed.get("resource"):
                rec["resource"] = parsed["resource"]
        else:
            rec["error_type"] = parsed.get("error_type")
            rec["error"] = parsed.get("error")
        leaf_results.append(rec)

        # ── f. capture ──
        cap = step.get("capture")
        if ok and isinstance(cap, dict):
            value = parsed.get("result")
            pointer = cap.get("pointer")
            if pointer is not None:
                found, value = resolve_pointer(value, pointer)
                if not found:
                    # 绝不猜字段：pointer 不存在就是明确的 step 失败
                    rec["ok"] = False
                    rec["error_type"] = "capture_pointer_not_found"
                    rec["error"] = f"capture pointer {pointer!r} 在 result 中不存在"
                    ok = False
                    failed_step = {"path": path, "op": op_id,
                                   "error_type": "capture_pointer_not_found"}
            if ok:
                var = cap["var"]
                env[var] = value
                capture_log.append({"path": path,
                                    "bindings": {k: v for k, v in env.items() if k != var},
                                    "var": var, "value": value})

        if not ok:
            failed_step = failed_step or {"path": path, "op": op_id,
                                          "error_type": rec.get("error_type")}
            if plan.on_error == "stop":
                status = "stopped_on_error"
                break

    finished = clock()
    summary = {
        "ok": status == "completed" and failed_step is None,
        "status": status,
        "completed": status == "completed" and failed_step is None,
        "run_id": run_id,
        "plan_version": plan.plan_version,
        "on_error": plan.on_error,
        "declared_leaf_count": plan.leaf_count,
        "leaf_count": len(leaf_results),
        "success_count": success,
        "failure_count": sum(1 for r in leaf_results if not r.get("ok")),
        "failed_step": failed_step,
        "started_at": t0.isoformat(timespec="seconds"),
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "duration_s": round(finished - started, 3),
        "leaf_results": leaf_results,
        "capture_log": capture_log,
        "warnings": warnings,
    }
    contention = _classify_warnings(warnings)
    if contention:
        summary["contention"] = contention
        summary["integrity"] = "contended"   # 提示：存在竞争，测量完整性无法证明
    if plan.execution_deadline_s is not None:
        summary["execution_deadline_s"] = plan.execution_deadline_s

    if artifact_dir is not None:
        try:
            d = Path(artifact_dir)
            d.mkdir(parents=True, exist_ok=True)
            p = d / f"batch_{run_id}.json"
            p.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str),
                         encoding="utf-8")
            summary["result_artifact"] = str(p)
        except Exception as e:                                   # noqa: BLE001
            summary.setdefault("warnings", []).append(
                {"step": None, "op": None, "message": f"写结果文件失败：{type(e).__name__}: {e}"})
    return summary
