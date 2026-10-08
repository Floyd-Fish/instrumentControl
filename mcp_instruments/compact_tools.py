"""compact profile 的 MCP 前端——5 个按需工具。

背景（docs/gpt_qa/2026-09-23-instrument-gateway-arch.md，方案 C 阶段 3/4）：
legacy profile 把 68 个工具定义全部塞进每次请求（实测 ≈19.8k token，占该 harness
静态注入的 44%），而多数会话根本不碰仪器。compact profile 只暴露 5 个稳定工具，
把"有哪些能力"变成**按需数据**：

    instr_devices()                     当前有哪些仪器（复用 legacy 的网络发现）
    instr_search(query, device, limit)  按关键词找操作
    instr_describe(ids)                 取某个操作的完整 schema/说明/安全属性
    instr_call(op, args)                执行一次操作
    instr_batch(plan)                   一次提交多个操作（扫频/批采/参数矩阵）

三点设计约定：

* **名字不与 legacy 冲突**：legacy 的 `instr_discover` 是"扫网段发现设备"，
  与"列出仪器"语义相近但参数不同；compact 下用 `instr_devices` 指代后者，
  避免两个 profile 之间出现同名不同义的陷阱。
* **执行语义完全复用 legacy**：`instr_call` 经注入的 `run_fn` 走同一个执行器
  （单 worker 串行、device_busy 闸门、墙钟预算），**不另起一条执行路径**——
  否则"compact 下超时行为不一样"这类差异会极难排查。`instr_batch` 同理：整批作为
  **一个** executor job 提交（见 instrument_runtime/batch.py 顶部第 1 条）。
* **安全不经前端**：`instr_call` / `instr_batch` 都不做放行判断，护栏仍在操作实现与
  policy 层（`instr_query`/`instr_write`/`dg_query` 自带黑名单，DG832 的保护联锁在库里）。
  所以绕过 compact 前端直接调也不会跳过任何门——这正是"护栏下沉"的意义（阶段 2）。
  前端只负责**入参校验**（含 plan 的结构/作用域 preflight），把错误在离开模型时就说清楚。
"""
from __future__ import annotations

import json
import re
from typing import Any, Awaitable, Callable

from instrument_runtime.validate import format_issues, validate_for_operation

__all__ = ["register", "COMPACT_TOOL_NAMES"]

#: compact profile 暴露的工具名（legacy 与之互斥，由 profile 决定注册哪一套）。
COMPACT_TOOL_NAMES = ("instr_devices", "instr_search", "instr_describe", "instr_call",
                      "instr_batch")

_MAX_DESCRIBE = 8   # 单次 describe 的操作数上限（防止一次拉回半张表）
_MAX_LIMIT = 20     # search 返回条数上限


def _json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


#: 中文 → 英文 token 的别名表（查询期扩展用）。
#:
#: 为什么需要它：工具名与参数是英文/缩写（`dmm_measure(function=volt_dc)`），而使用者用中文
#: 提问。只做子串匹配会让**切题的操作反而匹配不上**，而描述里恰好含某个中文词的无关操作
#: 却排到前面（实测 `万用表 电压` 会把 `dg.get_protect`——描述里有"电压"两字——排在
#: `dmm.measure` 之前，因为后者的参数叫 `volt_dc`）。所以查询时把中文词扩成英文 token。
_CJK_ALIASES: dict[str, tuple[str, ...]] = {
    # 设备族
    "示波器": ("sds", "dho", "mho", "scope"),
    "信号源": ("sdg", "dg"),
    "波形发生器": ("sdg", "dg"),
    "函数发生器": ("sdg", "dg"),
    "万用表": ("dmm", "ks3458a"),
    "八位半": ("ks3458a",),
    "电源": ("psu", "dh1766"),
    "仪器": ("instr",),
    # 动作
    "状态": ("status",),
    "快照": ("status",),
    "测量": ("measure", "meas"),
    "读取": ("read",),
    "读": ("read", "get"),
    "查询": ("query", "status"),
    "查": ("status", "query"),
    "设置": ("set", "configure"),
    "配置": ("configure", "set"),
    "输出": ("output", "outp"),
    "开关": ("output", "outp"),
    "复位": ("reset",),
    "保护": ("protect",),
    "扫频": ("sweep",),
    "截图": ("screenshot",),
    "波形": ("wave", "waveform", "shape"),
    "采样": ("sample", "acq"),
    "触发": ("trigger", "trig"),
    "通道": ("ch", "channel", "chan"),
    "时基": ("timebase",),
    "自动定标": ("auto_scale", "autoset"),
    "错误": ("error", "err"),
    "发现": ("discover",),
    # 量/参数
    "电压": ("volt", "vdc", "vac"),
    "电流": ("curr",),
    "频率": ("freq", "frequency"),
    "周期": ("period", "per"),
    "幅度": ("amp", "ampl"),
    "偏移": ("offset",),
    "相位": ("phase",),
    "峰峰": ("vpp", "pkpk"),
    "有效值": ("rms",),
    "电阻": ("res",),
}


def query_terms(query: str) -> list[str]:
    """把查询切成可匹配的词元，并把中文词**扩展**为对应的英文 token。

    为什么要 CJK 二元组（2026-09-23 实测缺陷）：原实现按**空白**切词，而中文没有空格——
    整句被当成一个词，永远匹配不上任何工具名或摘要，实测 `读信号源当前状态` 返回 0 条
    命中（同一时刻英文 `status` 返回 5 条），而使用者平时就用中文提问。

    为什么要别名扩展：二元组只解决"能不能匹配上中文描述"，解决不了"切题的操作里
    压根没有那个中文词"——见 `_CJK_ALIASES` 的注释。
    """
    q = (query or "").lower().strip()
    if not q:
        return []
    terms: list[str] = []
    for chunk in re.split(r"[\s,，、;；/|]+", q):
        if not chunk:
            continue
        for run in re.findall(r"[\u4e00-\u9fff]+", chunk):
            if len(run) == 1:
                terms.append(run)
            else:
                terms.extend(run[i:i + 2] for i in range(len(run) - 1))
            # 在 2~4 字的窗口里查别名（覆盖 电压/扫频/自动定标/波形发生器 这类词）
            for size in (2, 3, 4):
                for i in range(0, max(0, len(run) - size + 1)):
                    alias = _CJK_ALIASES.get(run[i:i + size])
                    if alias:
                        terms.extend(alias)
        terms.extend(re.findall(r"[a-z0-9_]+", chunk))
    seen: dict[str, None] = {}
    for t in terms:
        if t:
            seen.setdefault(t, None)
    return list(seen)


def _score(op, query: str) -> int:
    """极简关键词打分（68 个操作的规模不需要 embeddings，见讨论存档 §2.3）。

    每个字段的命中数**设有上限**：操作描述往往很长（本仓 docstring 动辄几百字），
    若不封顶，长描述会靠一堆偶然的二元组命中把精确匹配淹掉——实测中文查询
    `万用表 电压` 会把 `dg.get_protect` 排到 `dmm.measure` 前面，正是这个原因。
    """
    q = query.lower().strip()
    if not q:
        return 0
    if q == op.id.lower() or q == op.tool_name.lower():
        return 1000
    score = 50 if (q in op.id.lower() or q in op.tool_name.lower()) else 0
    name = op.tool_name.lower()
    summary = (op.summary or "").lower()
    description = (op.description or "").lower()
    device = (op.device or "").lower()
    # 参数名也要参与匹配：很多人是按"要设什么"来找的（"设频率" → freq → dg.set_wave），
    # 而操作名里未必有那个词（`dg_set_wave` 的频率参数就叫 `freq`）。
    params = " ".join(op.parameters).lower()
    terms = query_terms(q)
    name_hits = sum(1 for w in terms if w in name)
    sum_hits = min(sum(1 for w in terms if w in summary), 3)
    desc_hits = min(sum(1 for w in terms if w in description), 2)
    dev_hits = min(sum(1 for w in terms if w in device), 2)
    param_hits = min(sum(1 for w in terms if w in params), 2)
    kw_hits = sum(1 for w in terms if any(w == k for k in op.keywords))
    return (20 * name_hits + 8 * sum_hits + 3 * desc_hits + 10 * dev_hits
            + 6 * param_hits + 6 * kw_hits)


def register(mcp, *, registry, run_fn: Callable[[str, dict], Awaitable[str]],
             run_batch_fn: Callable[[dict], Awaitable[str]],
             devices_quick_fn: Callable[[], str],
             devices_op: str = "instr_discover") -> tuple[str, ...]:
    """把 compact 工具注册到给定的 FastMCP 实例，返回工具名元组。

    run_fn / run_batch_fn / devices_quick_fn 由 server.py 注入——它们负责用**既有执行器**
    或既有解析层取数；本模块不碰设备、不碰执行器。
    """

    @mcp.tool()
    async def instr_devices(full: bool = False) -> str:
        """列出仪器。

        full=False（默认，**推荐**）：**瞬时**返回已知地址 —— `devices.json` 配置 +
        `last_good_resources.json` 上次成功缓存。不做任何扫描。
        full=True：做**全量发现** —— 扫描本机各网段与所有 VISA 接口，能发现新接入或
        换了地址的仪器。代价很大：本机实测 **355.9s**（两个 /16 共 13 万台主机），
        期间会**占住设备执行器**（其它仪器调用一律返回 device_busy），而且很可能超过
        MCP 客户端默认 60s 的工具超时。

        何时需要 full=True：新接了仪器、仪器换了 IP/网段、或已知地址全部连不上。
        日常"我现在能用哪些仪器"用默认的快速路径即可。
        """
        if full:
            return await run_fn(devices_op, {})
        return devices_quick_fn()

    @mcp.tool()
    async def instr_search(query: str, device: str | None = None,
                           limit: int = 8) -> str:
        """搜索可用的仪器操作，返回规范 id 与一句话摘要（不返回完整参数表）。

        query: 自然语言或关键词，例如 "扫频 信号源"、"读信号源状态"、"measure vpp"；
               **留空 = 列出全部能力目录**（按设备族分组，只给操作 id）——不知道
               该用什么词时先用它枚举，再挑合适的 id 去 describe。
        device: 可选，限定设备族（dg832/sds/sdg/dmm/dho/mho/psu/ks3458a/instr/usb）；
        limit: 返回条数上限（1-20，默认 8）；目录模式（空 query）不受它限制。

        拿到 id 后用 `instr_describe` 取完整参数表，再用 `instr_call` 执行。
        """
        try:
            n = int(limit)
        except (TypeError, ValueError):
            return _json({"ok": False, "error_type": "param_validation",
                          "error": f"limit 必须是整数，收到 {limit!r}"})
        if not 1 <= n <= _MAX_LIMIT:
            return _json({"ok": False, "error_type": "param_validation",
                          "error": f"limit 超出范围 1-{_MAX_LIMIT}：{n}"})
        ops = list(registry)
        if device:
            ops = [o for o in ops if o.device == device]
        # 空查询 = **能力目录**（2026-09-23 加）。原来空查询恒返回 0 条，于是"我有哪些
        # 能力"这个问题在 compact 下无处可问——`instr_describe` 要你先知道 id，
        # `instr_search` 要关键词，模型一旦不知道设备族词汇就卡死。
        # 目录按设备族分组只回**操作 id**（不回首句摘要）：够用来接着 describe，
        # 又不会一次灌回半张表。
        if not (query or "").strip():
            groups: dict[str, list[str]] = {}
            for o in sorted(ops, key=lambda x: x.id):
                groups.setdefault(o.device, []).append(o.id)
            return _json({
                "ok": True, "mode": "catalog", "query": "", "device": device,
                "count": len(ops), "devices": groups,
                "hint": "这是全部可用操作（按设备族分组，只给 id）。用 instr_describe 取"
                        "某个操作的完整参数表与安全属性；给 query 传关键词可做排序搜索。",
            })
        ranked = sorted(((_score(o, query), o) for o in ops),
                        key=lambda p: (-p[0], p[1].id))
        hits = [o for s, o in ranked if s > 0][:n]
        if not hits:
            return _json({"ok": True, "query": query, "device": device, "count": 0,
                          "results": [],
                          "hint": "没有匹配的操作；用 instr_search 空查询可直接列出**全部**"
                                  "能力目录（按设备族分组），或换关键词/设备族名再试。"})
        return _json({
            "ok": True, "query": query, "device": device, "count": len(hits),
            "results": [{"id": o.id, "tool_name": o.tool_name, "device": o.device,
                         "risk": o.safety.risk,
                         "requires_confirm": o.safety.requires_confirm,
                         "summary": o.summary} for o in hits],
        })

    @mcp.tool()
    async def instr_describe(ids: str) -> str:
        """取操作的完整定义：参数表（JSON Schema）、说明、安全属性与关键约束。

        ids: 一个或多个操作 id / 工具名，逗号分隔，例如
             "sdg.set_wave" 或 "sds_measure,dmm_measure"（单次最多 8 个）。

        执行前应先 describe——说明里含该操作的强制前置条件与实测设备行为
        （例如 dg_protect 必须先于设幅度/开输出），跳过它会得到 protect_required 之类的拒绝。
        """
        wanted = [x.strip() for x in (ids or "").split(",") if x.strip()]
        if not wanted:
            return _json({"ok": False, "error_type": "param_validation",
                          "error": "ids 不能为空；传操作 id 或工具名，逗号分隔"})
        if len(wanted) > _MAX_DESCRIBE:
            return _json({"ok": False, "error_type": "param_validation",
                          "error": f"单次最多 {_MAX_DESCRIBE} 个，收到 {len(wanted)} 个"})
        found, missing = [], []
        for key in wanted:
            op = registry.get(key) or registry.by_id(key)
            if op is None:
                missing.append(key)
                continue
            found.append({
                "id": op.id, "tool_name": op.tool_name, "device": op.device,
                "summary": op.summary, "description": op.description,
                "parameters": dict(op.schema),
                "safety": {"risk": op.safety.risk,
                           "requires_confirm": op.safety.requires_confirm,
                           "raw_scpi": op.safety.raw_scpi,
                           "verify": list(op.safety.verify)},
                "notes": op.safety.note or None,
            })
        out: dict = {"ok": True, "count": len(found), "operations": found}
        if missing:
            out["not_found"] = missing
            out["hint"] = "未知 id；用 instr_search 找正确的 id。"
        return _json(out)

    @mcp.tool()
    async def instr_call(op: str, args: dict | None = None) -> str:
        """执行一次仪器操作。

        op: 操作 id 或工具名，例如 "sdg.set_wave"、"sds_measure"（先用 instr_search 找）；
        args: 参数字典，键名与 instr_describe 返回的 parameters 一致，例如
              {"ch": 1, "shape": "sine", "freq": 1000, "amp": 2.0}。

        省略有默认值的参数即用默认值；必填参数缺失或类型不符会**在本地直接拒绝**，
        不会下发到设备。返回值与 legacy profile 下同名工具完全一致（同一执行路径）。
        有副作用的操作（改输出/关机/复位）仍需按该操作的说明显式传 confirm=True。

        组合与循环（扫频、批量采集）请勿用本工具的多次调用堆叠——逐次调用会带来
        大量往返；那类任务属于后续阶段的 `instr_run`（受限代码执行）。
        """
        target = registry.get(op) or registry.by_id(op)
        if target is None:
            return _json({"ok": False, "error_type": "param_validation",
                          "error": f"未知操作 {op!r}；用 instr_search 查找正确的 id。"})
        payload = {} if args is None else args
        issues = validate_for_operation(target, payload)
        if issues:
            return _json({"ok": False, "error_type": "param_validation",
                          "error": f"入参校验未通过：{format_issues(issues)}",
                          "issues": [{"param": i.param, "message": i.message} for i in issues],
                          "expected": list((target.schema.get("properties") or {}).keys()),
                          "required": list(target.schema.get("required") or [])})
        return await run_fn(target.tool_name, dict(payload))

    @mcp.tool()
    async def instr_batch(plan: dict) -> str:
        """一次提交多个操作（扫频、批量采集、参数矩阵），由服务端循环执行。

        plan: Batch Plan DSL v1（版本化 JSON）。最小形态——
        {"plan_version": 1, "on_error": "stop", "max_leaf_steps": 100,
         "max_nesting_depth": 1, "execution_deadline_s": 120,
         "steps": [
           {"foreach": {"var": "f", "values": [100, 200]},
            "steps": [
              {"op": "sdg.set_wave", "args": {"ch": 1, "shape": "sine",
                                              "freq_hz": {"$var": "f"}, "amp_v": 2.0}},
              {"op": "dmm.measure", "args": {"function": "volt_ac"},
               "capture": {"var": "v", "source": "result"}}]}]}
        必填：plan_version / steps / on_error / max_leaf_steps / max_nesting_depth。
        on_error ∈ {stop, continue}；max_leaf_steps 是展开后的叶子数自有上限（服务器另有硬上限）。

        变量用**类型化引用** {"$var": "名字"}（不支持字符串模板）；capture 用整 result，
        或加 "pointer" 取 RFC 6901 JSON Pointer（如 "/value"）。作用域严格：foreach 变量
        只在其 body 内可见，capture 只对其后的同级步骤可见，**foreach 内捕获的值不能带到
        外层**（多轮迭代会有多个同名值）。作用域/未定义变量/重名/超限都在执行前一次性拒绝。

        整批作为**一个执行单元**提交：期间进程内其它设备调用得到 device_busy，不会插进
        序列中间（扫频需要的正是这个）。但它**不是事务**：不回滚，跨进程也不互斥。
        执行超时只在叶子边界停止后续步骤、不强杀已开始的那个。失败时**已产生的部分结果
        一并返回**——仪器侧副作用已经发生，隐瞒会让调用方无法判断设备当前状态。
        """
        if not isinstance(plan, dict):
            return _json({"ok": False, "error_type": "param_validation",
                          "error": f"plan 必须是 JSON 对象，收到 {type(plan).__name__}"})
        return await run_batch_fn(plan)

    return COMPACT_TOOL_NAMES
