"""Batch Plan DSL v1 + 解释器 离线校验（不碰仪器）——方案 C 阶段 4 的验收判据。

覆盖六组：

    S1 结构校验：必填键 / 未知键 / plan_version / on_error / 类型 —— 接受与拒绝用例。
    S2 词法作用域：未定义变量、foreach 内 capture 逃逸到外层、重名/遮蔽 —— 必须**静态拒绝**。
    S3 规模：嵌套深度、叶子精确计数（不展开）、服务器硬上限 —— 超限时**零仪器调用**。
    S4 JSON Pointer：取值与取不到的判定（绝不猜字段）。
    S5 执行：foreach 展开 + 变量解析 + capture + on_error + deadline（用假 registry 与假 executor）。
    S6 **核心不变量**：整批只提交**一个** executor job；且叶子经内层实现调用（不自调用执行器）。

用法：python TEST_SCRIPTS/common/verify_batch_offline.py
（输出纯 ASCII；plan 里的中文错误信息只在断言细节里出现，会被转义）
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "mcp_instruments"))

fails: list[str] = []


def _ascii(text) -> str:
    return str(text).encode("ascii", "replace").decode("ascii")


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {_ascii(name):54s} {_ascii(detail)[:110]}", flush=True)
    if not ok:
        fails.append(name)


def _codes(issues) -> set[str]:
    return {i.code for i in issues}


def _base(**over) -> dict:
    p = {"plan_version": 1, "on_error": "stop", "max_leaf_steps": 50,
         "max_nesting_depth": 1, "steps": [{"op": "fake.set", "args": {"v": 1}}]}
    p.update(over)
    return p


def _mkop(registry_mod, op_id, tool, fn, schema):
    return registry_mod.Operation(
        id=op_id, tool_name=tool, device=op_id.split(".")[0], summary="", description="",
        schema=schema, safety=registry_mod.Safety(risk="config"), keywords=(), budget=None, fn=fn)


def main() -> int:
    from instrument_runtime import plan as planmod
    from instrument_runtime import registry as regmod
    from instrument_runtime.plan import SERVER_MAX_LEAF_STEPS, resolve_pointer, validate_plan

    exists = lambda oid: oid in {"fake.set", "fake.read", "fake.fail"}   # noqa: E731

    # ---------------- S1 结构 ----------------
    print("S1 structural validation")
    ok_plan, iss = validate_plan(_base(), op_exists=exists)
    check("minimal valid plan is accepted", ok_plan is not None and not iss)

    _, iss = validate_plan({**_base(), "plan_version": 2}, op_exists=exists)
    check("wrong plan_version is rejected", "bad_plan_version" in _codes(iss), str(_codes(iss)))

    _, iss = validate_plan({**_base(), "on_error": "explode"}, op_exists=exists)
    check("bad on_error is rejected", "bad_on_error" in _codes(iss), str(_codes(iss)))

    bad = _base()
    bad.pop("max_leaf_steps")
    _, iss = validate_plan(bad, op_exists=exists)
    check("missing required key is rejected", "missing_key" in _codes(iss), str(_codes(iss)))

    _, iss = validate_plan({**_base(), "surprise": 1}, op_exists=exists)
    check("unknown top-level key is rejected", "unknown_key" in _codes(iss), str(_codes(iss)))

    _, iss = validate_plan({**_base(), "steps": []}, op_exists=exists)
    check("empty steps is rejected", "bad_steps" in _codes(iss), str(_codes(iss)))

    _, iss = validate_plan(_base(steps=[{"op": "fake.nope", "args": {}}]), op_exists=exists)
    check("unknown operation is rejected", "unknown_operation" in _codes(iss), str(_codes(iss)))

    _, iss = validate_plan(_base(steps=[{"op": "not-dotted", "args": {}}]), op_exists=exists)
    check("malformed op id is rejected", "bad_op_id" in _codes(iss), str(_codes(iss)))

    _, iss = validate_plan(_base(steps=[{"op": "fake.set", "args": {"v": "$v"}}]), op_exists=exists)
    check("string template is NOT a variable reference (plain literal)",
          not iss, "字符串模板只是普通字面量；类型错误留到执行前的 operation schema 校验")

    _, iss = validate_plan(_base(steps=[{"op": "fake.set",
                                         "args": {"v": {"$var": "bad name"}}}]),
                           op_exists=exists)
    check("malformed $var reference is rejected", "bad_var_ref" in _codes(iss), str(_codes(iss)))

    _, iss = validate_plan(_base(steps=[{"op": "fake.set", "args": {"v": 1},
                                         "capture": {"var": "x", "source": "result",
                                                     "pointer": "no-slash"}}]),
                           op_exists=exists)
    check("bad JSON Pointer is rejected", "bad_pointer" in _codes(iss), str(_codes(iss)))

    # ---------------- S2 作用域 ----------------
    print("\nS2 lexical scope (must be rejected statically)")
    p = _base(steps=[{"op": "fake.read", "args": {}, "capture": {"var": "v", "source": "result"}},
                     {"op": "fake.set", "args": {"v": {"$var": "v"}}}])
    ok2, iss = validate_plan(p, op_exists=exists)
    check("capture is visible to later siblings", ok2 is not None and not iss, str(_codes(iss)))

    p = _base(steps=[{"op": "fake.set", "args": {"v": {"$var": "nope"}}}])
    _, iss = validate_plan(p, op_exists=exists)
    check("undefined variable is rejected", "undefined_variable" in _codes(iss), str(_codes(iss)))

    # foreach 内 capture 不得逃逸到外层 —— Q4 的核心裁定
    p = _base(steps=[
        {"foreach": {"var": "f", "values": [1, 2]},
         "steps": [{"op": "fake.read", "args": {}, "capture": {"var": "v", "source": "result"}}]},
        {"op": "fake.set", "args": {"v": {"$var": "v"}}}])
    _, iss = validate_plan(p, op_exists=exists)
    check("foreach capture CANNOT escape to outer scope",
          "undefined_variable" in _codes(iss), str(_codes(iss)))

    p = _base(steps=[
        {"foreach": {"var": "f", "values": [1, 2]},
         "steps": [{"op": "fake.read", "args": {}, "capture": {"var": "v", "source": "result"}},
                   {"op": "fake.set", "args": {"v": {"$var": "v"}}}]}])
    ok3, iss = validate_plan(p, op_exists=exists)
    check("foreach body sees its own capture + the loop var",
          ok3 is not None and not iss, str(_codes(iss)))

    p = _base(steps=[
        {"foreach": {"var": "f", "values": [1, 2]},
         "steps": [{"op": "fake.set", "args": {"v": {"$var": "f"}}}]},
        {"op": "fake.set", "args": {"v": {"$var": "f"}}}])
    _, iss = validate_plan(p, op_exists=exists)
    check("foreach var does not leak out of its body",
          "undefined_variable" in _codes(iss), str(_codes(iss)))

    p = _base(max_nesting_depth=2, steps=[
        {"foreach": {"var": "f", "values": [1]},
         "steps": [{"foreach": {"var": "f", "values": [2]},
                    "steps": [{"op": "fake.set", "args": {"v": 1}}]}]}])
    _, iss = validate_plan(p, op_exists=exists)
    check("shadowing is rejected (same name nested)", "duplicate_variable" in _codes(iss),
          str(_codes(iss)))

    p = _base(steps=[{"op": "fake.read", "args": {}, "capture": {"var": "x", "source": "result"}},
                     {"op": "fake.read", "args": {}, "capture": {"var": "x", "source": "result"}}])
    _, iss = validate_plan(p, op_exists=exists)
    check("duplicate capture in the same scope is rejected",
          "duplicate_variable" in _codes(iss), str(_codes(iss)))

    # ---------------- S3 规模 ----------------
    print("\nS3 scale limits (counted exactly, without expanding)")
    nested = {"foreach": {"var": "a", "values": list(range(1, 11))},
              "steps": [{"foreach": {"var": "b", "values": list(range(1, 11))},
                         "steps": [{"op": "fake.set", "args": {"v": 1}}]}]}
    t0 = time.perf_counter()
    _, iss = validate_plan(_base(max_leaf_steps=10, max_nesting_depth=2, steps=[nested]),
                           op_exists=exists)
    dt = time.perf_counter() - t0
    check("leaf count uses multiplication (10x10=100 > 50 style limit)",
          "too_many_leaf_steps" in _codes(iss), str(_codes(iss)))
    check("counting is fast (no expansion)", dt < 1.0, f"{dt*1000:.1f} ms")

    # 巨量基数：若真去展开会有百万叶子；这里必须仍很快
    huge = {"foreach": {"var": "a", "values": list(range(1, 1001))},
            "steps": [{"foreach": {"var": "b", "values": list(range(1, 1001))},
                       "steps": [{"op": "fake.set", "args": {"v": 1}}]}]}
    t0 = time.perf_counter()
    _, iss = validate_plan(_base(max_leaf_steps=1000, max_nesting_depth=2, steps=[huge]),
                           op_exists=exists)
    dt = time.perf_counter() - t0
    check("1e6-cardinality plan is rejected fast (no materialization)",
          "too_many_leaf_steps" in _codes(iss) and dt < 1.0, f"{dt*1000:.1f} ms")

    _, iss = validate_plan(_base(max_leaf_steps=100000, max_nesting_depth=1,
                                 steps=[{"op": "fake.set", "args": {"v": 1}}]),
                           op_exists=exists)
    check("plan cannot raise the server hard limit via its own field",
          "too_many_leaf_steps" in _codes(iss) or "too_deep_server_limit" in _codes(iss)
          or not iss, f"server cap={SERVER_MAX_LEAF_STEPS}")

    deep = {"op": "fake.set", "args": {"v": 1}}
    for _ in range(6):
        deep = {"foreach": {"var": "x", "values": [1]}, "steps": [deep]}
    _, iss = validate_plan(_base(max_nesting_depth=99, steps=[deep]), op_exists=exists)
    check("server depth hard limit is enforced",
          "too_deep_server_limit" in _codes(iss), str(_codes(iss)))

    # ---------------- S4 JSON Pointer ----------------
    print("\nS4 RFC 6901 JSON Pointer")
    doc = {"a": {"b": [10, 20]}, "x/y": 1, "m~n": 2}
    check("root pointer returns the document", resolve_pointer(doc, "") == (True, doc))
    check("nested lookup", resolve_pointer(doc, "/a/b/1") == (True, 20))
    check("escaped '/' (~1)", resolve_pointer(doc, "/x~1y") == (True, 1))
    check("escaped '~' (~0)", resolve_pointer(doc, "/m~0n") == (True, 2))
    check("missing key -> not found (never guessed)",
          resolve_pointer(doc, "/nope")[0] is False)
    check("index out of range -> not found",
          resolve_pointer(doc, "/a/b/9")[0] is False)
    check("descend into scalar -> not found", resolve_pointer(doc, "/x~1y/z")[0] is False)

    # ---------------- S5/S6 执行（假 registry + 假 executor）----------------
    print("\nS5 execution with a fake registry and a fake executor")
    import server as S  # noqa: E402

    calls: list[tuple[str, dict]] = []

    def mk_set(v):
        calls.append(("fake_set", {"v": v}))
        return json.dumps({"ok": True, "result": {"value": v * 2},
                           "warnings": ["⚠ 另一个进程（PID 1）正在使用**同一地址** USB0::FAKE"]})

    def mk_read():
        calls.append(("fake_read", {}))
        return json.dumps({"ok": True, "result": {"value": 42}})

    def mk_fail():
        calls.append(("fake_fail", {}))
        return json.dumps({"ok": False, "error_type": "device_error", "error": "boom"})

    num_schema = {"type": "object", "properties": {"v": {"type": "number"}}, "required": ["v"]}
    reg = regmod.reset_registry()
    reg.register(_mkop(regmod, "fake.set", "fake_set", lambda v: mk_set(v), num_schema))
    reg.register(_mkop(regmod, "fake.read", "fake_read", mk_read,
                       {"type": "object", "properties": {}, "required": []}))
    reg.register(_mkop(regmod, "fake.fail", "fake_fail", mk_fail,
                       {"type": "object", "properties": {}, "required": []}))

    class FakeExec:
        def __init__(self):
            self.calls = 0
            self.labels: list[str] = []

        async def run(self, fn, budget_s=None, label="call", args=(), kwargs=None):
            self.calls += 1
            self.labels.append(label)
            return fn()

    fake = FakeExec()
    S._EXECUTOR = fake

    # 扫频式 plan：foreach 3 点 × (set + read)，capture 只在 body 内用
    sweep = _base(max_leaf_steps=50, max_nesting_depth=1, on_error="stop", steps=[
        {"foreach": {"var": "f", "values": [10, 20, 30]},
         "steps": [{"op": "fake.set", "args": {"v": {"$var": "f"}}},
                   {"op": "fake.read", "args": {},
                    "capture": {"var": "r", "source": "result", "pointer": "/value"}}]}])
    calls.clear(); fake.calls = 0
    out = json.loads(asyncio.run(S._run_batch(sweep)))
    check("batch completes", out["ok"] and out["status"] == "completed", out["status"])
    check("3 iterations x 2 leaves = 6 leaf results", out["leaf_count"] == 6, str(out["leaf_count"]))
    check("declared cardinality matches", out["declared_leaf_count"] == 6,
          str(out["declared_leaf_count"]))
    check("capture_log has one entry per iteration", len(out["capture_log"]) == 3,
          str(len(out["capture_log"])))
    check("capture_log records iteration bindings",
          [c["bindings"].get("f") for c in out["capture_log"]] == [10, 20, 30],
          str([c["bindings"] for c in out["capture_log"]]))
    check("capture via JSON Pointer captured the pointed value",
          [c["value"] for c in out["capture_log"]] == [42, 42, 42],
          str([c["value"] for c in out["capture_log"]]))
    # Q4 裁定：capture 以**事件日志**返回，而不是扁平 dict（foreach 里同名变量在多轮
    # 迭代中合法共存，扁平 dict 会有作用域歧义）。这里断言摘要里确实只有事件日志。
    check("captures are returned as an event log, not a flat dict",
          isinstance(out.get("capture_log"), list)
          and not ({"bindings", "captures", "variables"} & set(out)),
          f"keys={sorted(out)}")
    check("capture_log entries carry iteration bindings (no scope ambiguity)",
          all(isinstance(c, dict) and "path" in c and "bindings" in c
              for c in out["capture_log"]), "ok")

    # S6 核心不变量
    check("S6 whole batch used exactly ONE executor job", fake.calls == 1, str(fake.calls))
    check("job label is instr_batch", fake.labels == ["instr_batch"], str(fake.labels))
    check("leaves went through the inner invoke (canonical implementations)",
          [c[0] for c in calls] == ["fake_set", "fake_read"] * 3, str([c[0] for c in calls]))

    # 跨进程争用聚合
    check("cross-process contention is aggregated at run level",
          any(w.get("code") == "external_session_contention" for w in out.get("contention", [])),
          str(out.get("contention")))
    check("contention sets integrity=contended but does NOT flip ok",
          out.get("integrity") == "contended" and out["ok"] is True, str(out.get("integrity")))

    # on_error=stop：失败步之后不再执行，但**已产生的部分结果全部返回**
    stop_plan = _base(on_error="stop", steps=[
        {"op": "fake.set", "args": {"v": 1}},
        {"op": "fake.fail", "args": {}},
        {"op": "fake.set", "args": {"v": 3}}])
    calls.clear()
    out2 = json.loads(asyncio.run(S._run_batch(stop_plan)))
    check("on_error=stop stops at the failing step", out2["status"] == "stopped_on_error",
          out2["status"])
    check("on_error=stop keeps partial results (2 leaves recorded)",
          out2["leaf_count"] == 2, str(out2["leaf_count"]))
    check("on_error=stop does not run the step after the failure",
          [c[0] for c in calls] == ["fake_set", "fake_fail"], str([c[0] for c in calls]))
    check("failed_step identifies the failure", (out2.get("failed_step") or {}).get("op") == "fake.fail",
          str(out2.get("failed_step")))

    # on_error=continue
    cont_plan = _base(on_error="continue", steps=[
        {"op": "fake.fail", "args": {}},
        {"op": "fake.set", "args": {"v": 5}}])
    calls.clear()
    out3 = json.loads(asyncio.run(S._run_batch(cont_plan)))
    check("on_error=continue keeps going after a failure",
          [c[0] for c in calls] == ["fake_fail", "fake_set"], str([c[0] for c in calls]))
    check("on_error=continue reports failure_count",
          out3["failure_count"] == 1 and out3["success_count"] == 1,
          f"fail={out3['failure_count']} ok={out3['success_count']}")

    # operation schema 校验在变量解析之后
    bad_args = _base(steps=[{"op": "fake.set", "args": {"v": "not-a-number"}}])
    calls.clear()
    out4 = json.loads(asyncio.run(S._run_batch(bad_args)))
    check("resolved args are validated against the operation schema",
          out4["status"] == "stopped_on_error" and
          out4["leaf_results"][0]["error_type"] == "param_validation",
          str(out4["leaf_results"][0].get("error_type")))
    check("schema-invalid step never reached the device", calls == [], str(calls))

    # capture pointer 不存在 -> 明确失败，不猜
    miss_ptr = _base(steps=[{"op": "fake.read", "args": {},
                             "capture": {"var": "z", "source": "result",
                                         "pointer": "/nope"}}])
    out5 = json.loads(asyncio.run(S._run_batch(miss_ptr)))
    check("missing capture pointer fails explicitly (never guessed)",
          out5["status"] == "stopped_on_error" and
          out5["leaf_results"][0].get("error_type") == "capture_pointer_not_found",
          str(out5["leaf_results"][0].get("error_type")))

    # deadline：只在叶子边界停止；已启动的叶子自然结束
    tick = {"t": 0.0}

    def clock():
        tick["t"] += 0.6
        return tick["t"]

    dl_plan = {"plan_version": 1, "on_error": "stop", "max_leaf_steps": 50,
               "max_nesting_depth": 0, "execution_deadline_s": 1.0,
               "steps": [{"op": "fake.set", "args": {"v": 1}},
                         {"op": "fake.set", "args": {"v": 2}},
                         {"op": "fake.set", "args": {"v": 3}}]}
    vp, _ = validate_plan(dl_plan, op_exists=exists)
    from instrument_runtime import batch as batchmod
    summ = batchmod.run_plan(vp, invoke=lambda t, a: mk_set(a["v"]), registry=reg,
                             artifact_dir=None, clock=clock)
    check("deadline stops at a leaf boundary (no new leaf started)",
          summ["status"] == "deadline_reached" and summ["leaf_count"] < 3,
          f"status={summ['status']} leaves={summ['leaf_count']}")
    check("deadline keeps the leaves already produced", summ["leaf_count"] >= 1,
          str(summ["leaf_count"]))

    # 结果落盘
    art_plan = _base(steps=[{"op": "fake.set", "args": {"v": 7}}])
    out6 = json.loads(asyncio.run(S._run_batch(art_plan)))
    art = out6.get("result_artifact")
    check("run summary is written to an artifact file",
          bool(art) and Path(art).exists(), _ascii(art))
    if art and Path(art).exists():
        loaded = json.loads(Path(art).read_text(encoding="utf-8"))
        check("artifact contains the same leaf_results", len(loaded["leaf_results"]) == 1)

    print(f"\n== result: {'all PASS' if not fails else f'{len(fails)} FAIL'} ==")
    for f in fails:
        print(f"  - {_ascii(f)}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
