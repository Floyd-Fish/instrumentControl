# -*- coding: utf-8 -*-
from __future__ import annotations

"""
instrument MCP Server — 五台仪器的统一 MCP 接口。

设备库：sds_control(示波器) / sdg_control(信号源) / keysight_3446x(万用表) /
dho_control(示波器) / dh1766_control(电源)，经 common 统一发现层。
另有 keysight_3458a（HP/Keysight 3458A 八位半万用表，**非 SCPI**：`ID?`/`ERRSTR?`/
`RESET`/`TARM SGL,1`，走 GPIB 的 VISA 或本机 SICL 通路），工具前缀 `ks3458a_`。

启动: python mcp_instruments/server.py

地址解析：**server 内不写死任何 IP**。仪器地址随 DHCP/网段/换口/串口号变化，
各专用工具的 `resource` 参数默认 None，由 `_resolve()` 依次取
显式入参 → 环境变量 `INSTRUMENT_<KIND>_RES` → 用户配置 `devices.json`
→ 上次成功缓存 → `find_device()` 自动发现。详见 `_resolve` 上方注释。

安全约定：
    - 复位类命令不暴露（黑名单 confirm 也不放行）；
    - 远程锁定类命令（SYST:REM/RWL/LOCK/COMM:RLST）同样黑名单拦截，纯查询放行；
    - 关机/输出类工具需显式 confirm=True；
    - 每次调用连接→操作→关闭（无状态）+ 全局设备锁串行化。
"""
import asyncio
import atexit
import concurrent.futures
import contextlib
import functools
import inspect
import os
import re
import sys
import json
import csv
import queue
import time
import threading
import typing
from datetime import datetime
from pathlib import Path

# ── 运行环境预置：必须早于任何会拉起 numpy 的三方库 import ──────────────────
# 根因（2026-10-08 实测，证据 TEST_DATA/common/mem_probe_pyvisa_*.json）：
# pyvisa 1.16.2 的 `pyvisa/util.py` 在模块级 `import numpy`；本机 32 线程 CPU 上
# numpy 2.3.5(scipy-openblas64) 初始化 OpenBLAS 线程池按 ~23 MB/线程 calloc →
# `import pyvisa` 私有提交 +755 MB（驻留仅 ~20 MB）。MCP 进程内 numpy 只被
# pyvisa 用来解 IEEE block 二进制，单线程完全够用 → 锁 1 线程后 +15 MB。
# 用 setdefault 而非 set：允许启动方（计划任务 / OpenCode environment / 调试）覆盖。
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
for sub in ("dh1766_control/src",):
    sys.path.insert(0, os.path.join(ROOT, sub))

from mcp.server.fastmcp import FastMCP
from mcp import types as mcp_types

mcp = FastMCP("instruments")

# Codex（Windows）对同时声明 prompts+resources 能力的 stdio 服务器存在
# “工具发现成功但不注入会话”的缺陷（实测 kimi-cu 仅声明 tools 可正常注入，
# everything-mcp 与 instrument 声明 prompts 后均注入失败）。本服务器只用 tools，
# 移除 prompts/resources 处理器，使初始化能力收敛为仅 tools。
for _req_type in (
    mcp_types.ListPromptsRequest,
    mcp_types.GetPromptRequest,
    mcp_types.ListResourcesRequest,
    mcp_types.ListResourceTemplatesRequest,
    mcp_types.ReadResourceRequest,
):
    mcp._mcp_server.request_handlers.pop(_req_type, None)

_DEVICE_LOCK = threading.Lock()
_PREWARM_DONE = threading.Event()  # VISA/设备库冷启动完成前置位（看门狗放宽依据）


def _env_float(name: str, default: float) -> float:
    """读环境变量浮点覆盖值（坏值回退默认，配置写错不打断服务）。"""
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


# 看门狗与设备锁等待（设计见 _call 顶部；两者都可用环境变量覆盖，不必改码）：
#   INSTRUMENT_CALL_BUDGET_S：单次调用墙钟上限，超时放弃并报 timeout
#   INSTRUMENT_LOCK_WAIT_S  ：等设备锁的上限，超时报 device_busy（不再无限等）
# 默认 150s：须长于最长合法操作（SDS auto_scale 闭环最坏 ~7 档 ×(PKPK+FREQ 各 6s
# 超时) ≈ 90s+），否则会把正常慢操作误判成挂起。
_CALL_BUDGET_S = _env_float("INSTRUMENT_CALL_BUDGET_S", 150.0)
_LOCK_WAIT_S = _env_float("INSTRUMENT_LOCK_WAIT_S", 30.0)

# 设备专用工具返回体里回填"本次实际用的地址"：模型名 → 解析层的 kind
_MODEL_KIND = {"SDS": "sds", "SDG": "sdg", "DMM": "dmm", "DHO": "dho", "MHO": "mho",
               "DG832": "dg", "DH1766": "psu", "3458A": "ks3458a"}
_LAST_RESOLVED: dict[str, str] = {}  # kind -> 本次调用解析出的资源串（_DEVICE_LOCK 内更新）
# 连接失败时由 _ks3458a 填的"可执行诊断"（82357B/驱动预检查），_call 挂到返回体 hint 上
_LAST_DRIVER_HINT: dict[str, dict] = {}


_SESSION_LOCKS: set = set()      # 本进程占用过的资源（供退出时释放）


def _session_warnings(resource: str | None, model_name: str) -> list[str]:
    """刷新跨进程会话锁并取回"别人正在用"的告警（**绝不抛异常**，锁不阻断工具）。

    口径（用户指定 2026-09-17）：**判重按 VISA 地址**；同一台设备的**另一接口**
    （如 MHO 的 inst0 与 5555）单独给一条"跨接口并发未验证"的告警，不参与判重。
    详见 `common/session_lock.py` 与 `docs/visa_concurrency_20260917.md`。
    """
    if not resource:
        return []
    try:
        from common import session_lock

        _SESSION_LOCKS.add(resource)
        info = session_lock.touch(resource, kind=model_name)
        return list(info.get("warnings") or [])
    except Exception as e:                      # noqa: BLE001
        _log(f"会话锁检查跳过：{type(e).__name__}: {e}")
        return []


def _release_session_locks() -> None:
    """进程退出时释放本进程占用的会话锁（失败也无妨：TTL/pid 判据会兜住）。"""
    try:
        from common import session_lock

        for r in list(_SESSION_LOCKS):
            session_lock.release(r)
    except Exception:
        pass


atexit.register(_release_session_locks)


def _out_res(model_name: str, resource: str | None) -> str | None:
    """输出用资源串：显式传入优先，否则回填本次解析结果（仅设备类工具）。"""
    return resource or _LAST_RESOLVED.get(_MODEL_KIND.get(model_name, ""))

# ============ 设备地址解析（不写死 IP）============
#
# 实现已下沉到 `common/resolver.py`：MCP 服务器与 `TEST_SCRIPTS/` 共用同一套
# 地址来源（避免"两套缓存/两套键"）。解析顺序：
#   ① 工具入参 resource（显式指定）
#   ② 环境变量 INSTRUMENT_<KIND>_RES（如 INSTRUMENT_SDS_RES）
#   ③ 用户配置 <配置目录>/devices.json（{"sds": "TCPIP0::…::inst0::INSTR", …}）
#   ④ 上次成功缓存 <配置目录>/last_good_resources.json
#      （instr_discover 发现成功后按 *IDN? 回写；连接成功后也会回写）
#   ⑤ 自动发现 common.find_device(idn_contains=…, allow_scan=False)
#      （只查 VISA 已注册资源；LAN 未注册设备请先跑 instr_discover；
#        需要自动扫描可设 INSTRUMENT_ALLOW_SCAN=1）
# 这里保留与既有调用点一致的别名，实现细节见 common/resolver.py。
from common.resolver import (  # noqa: E402
    DEVICE_KINDS,
    idn_kind as _idn_kind,
    known_resources as _known_resources,
    remember as _remember,
    remember_candidates as _remember_candidates,
    resolve as _resolve,
    resource_rank as _resource_rank,
)


def _pair_readback(cmd: str | None, resp: str | None) -> list[dict] | None:
    """多段回读的**字段配名**：':C1:SCALe?;:C1:OFFSet?' → [{cmd,value}, {cmd,value}]。

    现场教训（docs/tool_optimization_20260915.md §P2-6）：`;` 串联的多条回读原先
    只把响应原样拼成 "a;b" 返回——没有字段名，调用方要自己数字段、**写错顺序不报错**。
    这里按查询段与返回段一一配对；段数不符时如实说明（不硬配）。
    """
    if not cmd or resp is None or ";" not in cmd:
        return None
    units = [u.strip() for u in cmd.split(";") if u.strip()]
    vals = [v.strip() for v in resp.split(";")]
    if len(units) != len(vals):
        return [{"cmd": cmd, "value": resp,
                 "note": f"段数不符（查询 {len(units)} 段 / 返回 {len(vals)} 段），未配对名"}]
    return [{"cmd": u, "value": v} for u, v in zip(units, vals)]


def _ok(model, result, resource=None):
    d = {"ok": True, "model": model, "result": result}
    if resource:
        d["resource"] = resource
    return json.dumps(d, ensure_ascii=False, default=str)


def _err(error_type, msg, model=None, resource=None):
    d = {"ok": False, "error_type": error_type, "error": msg}
    if model:
        d["model"] = model
    if resource:
        d["resource"] = resource
    return json.dumps(d, ensure_ascii=False, default=str)


class ToolDiagnosis(Exception):
    """带**结构化诊断字段**的工具异常：错误分类照旧（error_type），另把
    suspicious/hint/window/evidence 等诊断键并进返回体。

    现场教训（docs/tool_optimization_20260915.md §P1-4）："无有效值"把
    **离屏 / 屏内边沿不足 / 没接信号** 三种完全不同的原因混成一句话，误导排查方向。
    诊断字段让调用方直接看到判据、窗口和建议，不必先截图猜。
    """

    def __init__(self, error_type: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.error_type = error_type
        self.details = details or {}


def _call(model_name, connect_fn, fn, close_fn=None, resource=None,
          lock_wait_s=None):
    """**同步执行体**：持锁 → 连接 → 操作 → 关闭，错误分类。

    只被 `_DeviceExecutor` 的 worker 线程调用（经 `@device_tool` 装饰器），
    **不要**在事件循环线程里直接调它——它会阻塞。

    设备锁：单 worker 下正常情况下没有竞争者，此处 `acquire(timeout)` 只作
    第二道防线（防止将来有代码绕过 executor 直接碰 VISA）——真要等满 timeout，
    恰恰说明有这个 bug 或异常路径长期占锁，是有价值的告警信号。
    """
    wait = _LOCK_WAIT_S if lock_wait_s is None else lock_wait_s
    if not _DEVICE_LOCK.acquire(timeout=max(0.0, wait)):
        return _err("device_busy",
                    f"设备锁被占用超过 {wait:.0f}s（有路径绕过统一执行器，或异常"
                    "路径长期占锁）；本次未向任何设备下发命令。重启 MCP 服务可恢复。",
                    model_name, _out_res(model_name, resource))
    try:
        try:
            dev = connect_fn()
        except Exception as e:
            payload = _err("connection", f"{type(e).__name__}: {e}", model_name,
                           _out_res(model_name, resource))
            # 3458A 专用：把 82357B/驱动预检查结论作为 hint 一并回传（缺驱动→提示装
            # Keysight IO Libraries Suite），避免只抛一句超时/库加载失败。
            hint = _LAST_DRIVER_HINT.pop("ks3458a", None) if model_name == "3458A" else None
            if hint:
                try:
                    d = json.loads(payload)
                    d["hint"] = hint
                    payload = json.dumps(d, ensure_ascii=False, default=str)
                except Exception:                    # noqa: BLE001
                    pass
            return payload
        res = _out_res(model_name, resource)
        # 跨进程**咨询锁**（2026-09-17）：本进程内已有单 worker 串行化，但**跨进程不互斥**——
        # 同一台仪器被两个实例同时操作会**静默串台**（实测：互相读到对方的响应）。
        # 这里刷自己的占用 + 把他人占用作为 warning 附在返回体里；只告警、不阻塞。
        warns = _session_warnings(res, model_name)

        def finish(payload: str) -> str:
            """把会话告警挂到返回体上（不改动原有字段，新增 warnings 为可选键）。"""
            if not warns:
                return payload
            try:
                d = json.loads(payload)
                d["warnings"] = list(d.get("warnings") or []) + warns
                return json.dumps(d, ensure_ascii=False, default=str)
            except Exception:
                return payload

        try:
            return finish(_ok(model_name, fn(dev), res))
        except ToolDiagnosis as e:
            # 诊断字段与错误一起返回：调用方一眼看到"为什么没读数 + 该改什么"
            d = json.loads(_err(e.error_type, str(e), model_name, res))
            d.update(e.details)
            return finish(json.dumps(d, ensure_ascii=False, default=str))
        except ValueError as e:
            return finish(_err("param_validation", str(e), model_name, res))
        except RuntimeError as e:
            return finish(_err("device_error", str(e), model_name, res))
        except Exception as e:
            return finish(_err("communication", f"{type(e).__name__}: {e}", model_name, res))
        finally:
            try:
                if close_fn is not None:
                    close_fn(dev)
                elif hasattr(dev, "close"):
                    dev.close()
            except Exception:
                pass
    finally:
        _DEVICE_LOCK.release()


# ============ 异步调用边界（单 worker 执行器）============
#
# 为什么要有这一层（2026-09-15 定稿，设计评审见 docs/gpt_qa/20260915-mcp-async-refactor.md）：
#
# FastMCP 对**同步**工具直接 `fn(**args)` 跑在事件循环线程上（读 SDK 源码确认，
# 1.27.0 的 tools 分支没有 to_thread）。而设备 I/O 是阻塞的、最坏约 90s（auto_scale）
# → 整个事件循环被冻结那么久：协议层收不到/发不出任何消息，超时也无法在等锁阶段生效。
#
# 本层把「异步」收敛到**唯一一处**（不改 50 个工具的实现）：
#     MCP(事件循环) → admission 闸门 → 单 worker 线程 → 原来的同步函数 → VISA
#
# 三条核心语义（都是实测教训的产物）：
#   1. 设备占用状态绑定**真正执行 VISA 的 worker**，不绑定等待结果的协程——
#      否则协程超时返回后锁就没了，下一个请求会与仍在跑的 VISA 并发（危险）；
#   2. MCP 超时 ≠ 操作终止：超时只是「调用方不再等待」，worker 继续跑、设备保持
#      BUSY，后续请求立即拿到 device_busy（而不是白等一轮锁超时）；
#   3. BUSY 只在 worker 的 Future 真正 done 时清除（在事件循环线程里清）。
#
# 局限（如实记录）：超时后 worker 仍会跑完——Python 无法安全强杀线程。若某天真
# 需要「到点无条件恢复」，只能把 VISA 挪进子进程再 kill（另立课题）。

_EXECUTOR: "_DeviceExecutor | None" = None
_STDOUT_FIXED = False


def _fix_stdout_once() -> None:
    """把 `sys.stdout` 永久指向 stderr（只做一次，首次工具调用时）。

    为什么需要：stdio 模式下 stdout 是 JSON-RPC 协议通道，底层库（本仓库历史代码、
    pyvisa 等）可能 `print()`，污染协议流会让客户端直接断开（2026-09-15 实测过）。

    为什么是「首次工具调用时」而不是启动时：`mcp/server/stdio.py` 在 `mcp.run()`
    内部才执行 `TextIOWrapper(sys.stdout.buffer)` 捕获协议流。若在 `run()` 之前就
    改 `sys.stdout`，捕获到的就是 **stderr 的 buffer** → 协议写到 stderr → 服务器
    等于死了。首次工具调用必然晚于 transport 绑定，是最晚可证的时机（时序分析见
    评审文档 Q3-①）。检查与赋值之间没有 await，单事件循环下无需加锁。

    局限：只覆盖 Python 层经 `sys.stdout` 的输出；`os.write(1, ...)` 或 native 库
    直接写 fd 1 的不受影响（但那些也改不了——`os.dup2(2, 1)` 会把协议 writer 的
    底层 fd 一起换掉，绝不能做）。
    """
    global _STDOUT_FIXED
    if _STDOUT_FIXED:
        return
    _STDOUT_FIXED = True
    if sys.stdout is not sys.stderr:
        sys.stdout = sys.stderr


# 2026-09-22：stderr 编码在**导入时**就固定成 UTF-8。
# 为什么：中文 Windows 下 Python 默认按 GBK(cp936) 写 stderr，而 MCP 客户端（DSH 等）
# 把子进程 stderr 当 UTF-8 文本读 → 中文/箭头变成替换字符 U+FFFD（现场症状：终端侧
# xterm.js `Parsing error: code 65533` 噪音 + traceback 路径行被吃掉半截）。
# `errors="backslashreplace"` 顺带消掉"日志里有不可编码字符 → UnicodeEncodeError 打断
# 输出"这一类故障；异常文本另外用 ascii() 转义（见 __main__ 自检）。
# 与 `_fix_stdout_once` 的区别：只改现有 stderr wrapper 的编码，不动对象、不动 fd、不动
# `sys.stdout.buffer`，因此不可能影响 mcp.run() 后面自建的协议流 → 可以在导入时执行。
try:
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
except Exception:  # 非 TextIOWrapper / 已被替换（pytest、自定义流）时静默跳过
    pass


class _DeviceExecutor:
    """单 worker 设备执行器：准入闸门 + 墙钟 deadline + 线程封闭。

    - 所有设备 I/O 只在这一个 **daemon** worker 线程里发生（thread confinement，
      比"多线程抢一把锁"更容易推理）；
    - BUSY 期间新请求**立即**返回 device_busy，不排队、不白等；
    - deadline 到期只放弃等待，不清 BUSY（worker 还在跑，见模块级说明）。

    为什么**不用** `concurrent.futures.ThreadPoolExecutor`（2026-09-16 设计复核，
    实验证据见 `TEST_SCRIPTS/common/verify_executor_exit.py`）：
    TPE 的 worker 线程是**非 daemon**，且 `concurrent.futures.thread` 注册了
    `atexit` 钩子会 **join** 它们。这套设计要顶住的恰恰是"驱动挂起、worker 卡在
    native 调用里"——那种情形下 TPE 会让**整个进程无法退出**（实测：提交一个
    `sleep(600)` 任务后进程 12s 内不退出；同场景 daemon 线程秒退），
    于是"重启 MCP 服务可恢复"这条恢复路径反而失效，只能 `taskkill /F`。
    daemon worker + queue 保留全部既有语义（单线程封闭、BUSY 闸门、超时不取消
    worker、结果/异常原样回填），但**进程随时可退**。
    """

    def __init__(self, default_budget_s: float):
        self._default_budget = default_budget_s
        self._busy = False          # 只在事件循环线程读写（见 _finish_on_loop）
        self._started_at = 0.0
        self._jobs: "queue.Queue" = queue.Queue()
        threading.Thread(target=self._worker, daemon=True,
                         name="visa-worker").start()

    def _worker(self) -> None:
        """唯一执行设备 I/O 的线程：取一个任务 → 跑 → 把结果/异常交回事件循环。"""
        while True:
            fn, done, loop = self._jobs.get()
            try:
                result = fn()
            except BaseException as e:              # noqa: BLE001
                self._finish_on_loop(loop, done, e, ok=False)
            else:
                self._finish_on_loop(loop, done, result, ok=True)

    def _finish_on_loop(self, loop, done, payload, ok: bool) -> None:
        """回填结果并清 BUSY——**在事件循环线程里执行**（保持 `_busy` 单线程写）。

        顺序有意：先清 BUSY 再 set_result，调用方拿到结果后立刻重试时看到的是
        "空闲"（与旧实现的语义一致）。事件循环已关闭（进程收尾）时静默放弃。
        """
        def _apply() -> None:
            self._busy = False
            if done.done() or done.cancelled():
                return
            if ok:
                done.set_result(payload)
            else:
                done.set_exception(payload)

        try:
            loop.call_soon_threadsafe(_apply)
        except RuntimeError:
            pass

    def _budget_for(self, budget_s, args, kwargs) -> float:
        """预算解析：None→默认；数值→直接用；可调用→按本次入参算（通用工具的预算
        随 `timeout_ms` 变，需要这种动态形式）。预热未完成（VISA 冷启动实测
        30-40s）再放宽 90s，防误杀首调用。"""
        if budget_s is None:
            b = self._default_budget
        elif callable(budget_s):
            b = budget_s(args, kwargs)
        else:
            b = budget_s
        b = float(b)
        if not _PREWARM_DONE.is_set():
            b += 90.0
        return b

    async def run(self, fn, budget_s=None, label: str = "call",
                  args=(), kwargs=None) -> str:
        budget = self._budget_for(budget_s, args, kwargs or {})

        # 检查与置位之间没有 await → 对同一事件循环内的其它协程是原子的
        if self._busy:
            waited = time.monotonic() - self._started_at
            return _err("device_busy",
                        f"设备正被另一个调用占用（已运行 {waited:.0f}s），本次未向任何"
                        "设备下发命令、也未排队。等它结束后重试；若它已超时仍长时间"
                        "BUSY，说明底层驱动真的卡死了——重启 MCP 服务可恢复。",
                        label)
        self._busy = True
        self._started_at = time.monotonic()

        loop = asyncio.get_running_loop()
        done: "asyncio.Future" = loop.create_future()
        try:
            self._jobs.put((fn, done, loop))
        except BaseException:
            self._busy = False
            raise

        try:
            # 超时只取消"等待"（daemon worker 继续跑完，把 VISA 会话收干净）——
            # 与旧实现 `asyncio.shield(fut)` 的语义一致，但不受 atexit join 拖累。
            return await asyncio.wait_for(done, timeout=budget)
        except asyncio.TimeoutError:
            elapsed = time.monotonic() - self._started_at
            _log(f"{label} 超过 deadline {budget:.0f}s（已运行 {elapsed:.0f}s）——"
                 f"放弃等待；worker 仍在跑，设备保持 BUSY 直到它真正结束")
            return _err("timeout",
                        f"等待超过 {budget:.0f}s 已放弃（设备离线/驱动挂起）。注意："
                        "超时只代表不再等待，操作**可能仍在执行或随后成功**——"
                        "有副作用的命令请回读确认；期间其它设备调用会返回 device_busy。",
                        label)


def _log(msg: str) -> None:
    """服务端日志走 stderr（stdout 是协议通道）。"""
    print(f"[instruments] {msg}", file=sys.stderr, flush=True)


def device_tool(budget_s=None):
    """注册一个 MCP 工具，并把它接入统一异步执行器。

    用法与 `@device_tool()` 完全一致，只是把装饰器换成本函数：
        @device_tool()                    # 用默认预算
        @device_tool(budget_s=300.0)      # 实测确认的慢操作
        @device_tool(budget_s=lambda kw: ...)  # 按入参动态算预算

    返回**原同步函数**（不是包装器）：模块内其它代码直接调用 `foo()` 时行为不变，
    只有 MCP 注册表持有 async 包装——对既有代码侵入最小。
    """
    def deco(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            _fix_stdout_once()
            return await _executor().run(functools.partial(fn, *args, **kwargs),
                                         budget_s, label=fn.__name__,
                                         args=args, kwargs=kwargs)
        _preserve_signature(wrapper, fn)   # 保 50 个工具的入参 schema 不变
        mcp.tool()(wrapper)
        return fn
    return deco


def _preserve_signature(wrapper, fn) -> None:
    """让包装器的签名/注解与原函数逐字一致，保证 FastMCP 生成的 JSON Schema 不变。

    本模块启用了 `from __future__ import annotations`，所以注解都是**字符串**，
    FastMCP 走 `inspect.signature(func, eval_str=True)` 解析。`functools.wraps`
    通常够（同模块 globals 相同），但这里显式解析并固定一次，消除"将来把工具挪到
    别的模块"或 SDK 改用 `get_type_hints()` 时的静默漂移。
    """
    try:
        hints = typing.get_type_hints(fn, include_extras=True)
    except Exception:
        hints = {}
    sig = inspect.signature(fn)
    params = [p.replace(annotation=hints.get(n, p.annotation))
              for n, p in sig.parameters.items()]
    wrapper.__signature__ = sig.replace(
        parameters=params,
        return_annotation=hints.get("return", sig.return_annotation))
    if hints:
        wrapper.__annotations__ = dict(hints)


def _executor() -> _DeviceExecutor:
    """惰性取执行器（首次调用时创建，避免 import 期就占一个线程）。"""
    global _EXECUTOR
    if _EXECUTOR is None:
        _EXECUTOR = _DeviceExecutor(_CALL_BUDGET_S)
    return _EXECUTOR



def _verify_idn(kind: str, resource: str, idn: str | None) -> None:
    """连接后核对 *IDN? 是否确为目标设备类（地址可能已被 DHCP 回收给别的设备）。

    配置/缓存里的地址迟早会过期——若该 IP 现在属于另一台设备，继续下发 SCPI
    就是"对未知设备操作"（可能改掉别人的仪器设置）。核对不通过立即断开并报错，
    提示重新发现；核对通过才允许把地址写回缓存。
    """
    token, label, _env = DEVICE_KINDS[kind]
    if not idn or token.upper() not in idn.upper():
        raise RuntimeError(
            f"地址校验失败：{resource} 上的设备 *IDN? = {idn!r}，"
            f"与目标设备（{label}，期望含 {token!r}）不符——地址可能已被 DHCP "
            f"分配给其它设备。请先调用 instr_discover 重新发现，"
            f"或用 config_cli.py 更正本机配置（devices.json）。"
        )


def _sds(resource: str | None = None) -> SDS:
    from sds_control import SDS

    res = _resolve("sds", resource)
    _LAST_RESOLVED["sds"] = res  # 供返回体回填本次实际地址
    s = SDS(res)
    s.connect()
    _verify_idn("sds", res, s.idn())
    _remember("sds", res)
    return s


def _sdg(resource: str | None = None) -> SDG:
    from sdg_control import SDG

    res = _resolve("sdg", resource)
    _LAST_RESOLVED["sdg"] = res  # 供返回体回填本次实际地址
    g = SDG(res)
    g.connect()
    _verify_idn("sdg", res, g.idn())
    _remember("sdg", res)
    return g


def _dmm(resource: str | None = None) -> DMM:
    from keysight_3446x import DMM

    res = _resolve("dmm", resource)
    _LAST_RESOLVED["dmm"] = res  # 供返回体回填本次实际地址
    d = DMM(res)
    d.connect()
    _verify_idn("dmm", res, d.idn())
    _remember("dmm", res)
    return d


def _dho(resource: str | None = None) -> DHO:
    from dho_control import DHO

    res = _resolve("dho", resource)
    _LAST_RESOLVED["dho"] = res  # 供返回体回填本次实际地址
    h = DHO(res)
    h.connect()
    _verify_idn("dho", res, h.idn())
    _remember("dho", res)
    return h


def _mho(resource: str | None = None) -> MHO:
    from mho_control import MHO

    res = _resolve("mho", resource)
    _LAST_RESOLVED["mho"] = res  # 供返回体回填本次实际地址
    m = MHO(res)
    m.connect()
    _verify_idn("mho", res, m.idn())
    _remember("mho", res)
    return m


def _dg(resource: str | None = None, model: str | None = None) -> DG832:
    """DG832 连接（库自带 model 注册表；地址走解析层 kind=dg）。"""
    from dg832_control import DG832

    res = _resolve("dg", resource)
    _LAST_RESOLVED["dg"] = res  # 供返回体回填本次实际地址
    g = DG832(resource=res, **(dict(model=model) if model else {}))
    g.connect()
    _verify_idn("dg", res, g.idn())
    _remember("dg", res)
    return g


def _psu_connect(resource: str | None = None) -> DH1766:
    """DH1766 连接（DH1766 类无 close，退出经 _psu_close 关 client）。"""
    from dh1766_control import DH1766
    from dh1766_control.visa import VisaClient

    res = _resolve("psu", resource)
    _LAST_RESOLVED["psu"] = res  # 供返回体回填本次实际地址
    p = DH1766(VisaClient(res, timeout_ms=5000))
    _verify_idn("psu", res, p.idn())
    _remember("psu", res)
    return p


def _psu_close(p: DH1766) -> None:
    """DH1766 会话收尾：先把面板控制权还给现场，再关连接。

    2026-09-13 实测（V0.1.4.3）：**任何远程会话都会把电源置为 REM（远程模式）**
    ——`SYST:COMM:RLST?` 在新会话的第一条命令即返回 'REM'（此前手册/库用的
    `SYST:COMM:RLST:STAT?` 在本机无响应，已修正）。REM 下现场面板可能不可操作，
    故每次 DH1766 工具调用结束都补发一次 `SYST:LOC`（LOC 只归还面板控制权，
    **不改动输出/电压/模式**；同一会话内后续 SCPI 查询实测仍正常）。
    """
    try:
        if getattr(p, "client", None) is not None:
            p.local()
    except Exception:
        pass
    try:
        if getattr(p, "client", None) is not None:
            p.client.close()
    except Exception:
        pass


def _ks3458a(resource: str | None = None, timeout_s: float = 30.0) -> "DMM3458A":
    """3458A 连接（专用库 keysight_3458a；地址走解析层 kind=ks3458a）。

    **默认不改设备状态**（`reset_on_open=False`）：MCP 是无状态工具，不该每次调用都
    复位仪表。要重置走 `ks3458a_reset`（需 confirm=True）。

    连接时会做一次**会话恢复**（IFC/clear → 有限 drain → TARM HOLD/TRIG HOLD）：
    上次会话可能把表留在 free-run（持续吐读数、不理查询），不恢复就会读到错位数据。
    恢复不发 RESET、不改档位/NPLC；但 IFC/Device Clear 会中断**整条 GPIB 总线**上的
    活动（共享实验台上别人正在采集时要注意）。随后 `prepare_for_read()` 补
    `END ALWAYS`/`INBUF ON`/`TRIG AUTO`（否则上次留下的 TRIG HOLD 会让读数永不出数）。

    **连接失败时**：自动附带 82357B 的 USB/驱动预检查结论（`hint`），缺驱动时直接
    告诉用户装 Keysight IO Libraries Suite——而不是抛一句超时（AGENTS.md：可执行结论）。
    """
    from keysight_3458a import DMM3458A

    res = _resolve("ks3458a", resource)
    _LAST_RESOLVED["ks3458a"] = res  # 供返回体回填本次实际地址
    # **默认走进程外 worker**（2026-09-23 实测结论）：MCP 是长驻进程，别的仪器工具用 pyvisa
    # 会把**系统 VISA**（System32\visa32.dll，IVI 壳）加载进来，此后本库的 Keysight VISA 通路
    # 会串味——轻则 `viWrite` `VI_ERROR_INV_OBJECT`，重则 `viOpen` 访问违例（实测 MCP 里就是
    # 后者）。子进程只加载 Keysight 栈 ⇒ 干净；且卡在 ioGPIB 内时父进程能 kill 回收句柄。
    # 需要回退进程内（排障用）：设 `INSTRUMENT_KS3458A_WORKER=0`。
    d = None
    if os.environ.get("INSTRUMENT_KS3458A_WORKER", "1").lower() not in ("0", "false", "no"):
        try:
            from keysight_3458a.remote import RemoteDMM

            d = RemoteDMM(res, timeout_s=timeout_s,
                          deadline_s=min(240.0, max(30.0, float(timeout_s) * 2)))
        except Exception as exc:                    # noqa: BLE001
            sys.stderr.write(f"[3458A] worker 不可用，回退进程内：{exc}\n")
            d = None
    if d is None:
        d = DMM3458A(res, timeout_s=timeout_s, reset_on_open=False)
    try:
        d.connect()
    except Exception:
        try:
            from keysight_3458a.driver_check import check_gpib_driver

            info = check_gpib_driver()
            if not info.get("ok"):
                _LAST_DRIVER_HINT["ks3458a"] = {
                    "verdict": info.get("verdict"),
                    "message": info.get("message"),
                    "devices": info.get("devices"),
                }
        except Exception:                            # noqa: BLE001 —— 诊断失败不掩盖原错
            pass
        raise
    # 3458A 没有 *IDN?，用 ID?（返回含 3458）——同一套身份校验口径
    _verify_idn("ks3458a", res, d.idn())
    _remember("ks3458a", res)
    return d


def _ks3458a_error(d) -> dict:
    """读 3458A 错误队列并判读（**没有 SYST:ERR?，只有 `ERRSTR?`**）。

    3458A 的写操作**没有回读通道**（无 `DCV?`/`NPLC?`），`ERRSTR?` 是"设备接受了
    这条命令"的唯一证据，所以每个改配置的工具都把它带回返回体。
    """
    from keysight_3458a import is_error_clear

    raw = d.error_string()
    return {"errstr": raw, "error_clear": is_error_clear(raw)}


# ============ 发现 ============

@device_tool(budget_s=300.0)   # 多网段扫描比单网段慢，给它更宽裕的等待上限
def instr_discover(cidr: str | None = None) -> str:
    """发现本机所有仪器（LAN 网段 + USB/GPIB/串口），结果自动回写地址缓存——
    之后各专用工具不传 resource 也能连上。

    cidr 可选；省略时扫本机所有接口所在的私网网段（多网卡/挂 VPN/代理 TUN 都能覆盖）。
    换了网段、换了 USB 口/串口号，或专用工具报连接失败时，先重跑本工具。

    返回体五组字段（lan / visa / resolved / recognised_now / psu_local_restored）
    的语义见 skill instrument-mcp →「工具选择决策树」。
    """
    import ipaddress
    import concurrent.futures as cf
    from common.discovery import (
        detect_cidr,
        forget_hanging_ports,
        local_cidrs,
        local_scan_segments,
        proxy_likely,
        identify,
        identify_all,
        identify_lan,
        identify_lan_all,
        list_resources,
        probe_alive,
        probe_alive_many,
        probe_open_ports,
        probe_serials_isolated,
    )

    # 显式重新发现：清空"挂起串口"缓存，允许重新探测这些口（自动发现路径会跳过
    # 它们以省时；只有用户主动调本工具时才值得重试一遍）
    forget_hanging_ports()

    def fn(_):
        # VISA 资源（USB/串口/GPIB）先探测——不依赖网段，代理干扰不影响
        def make_entry(r: str, idn) -> dict:
            """非串口 VISA 资源条目（USB/GPIB 走 VISA，有 open_timeout 兜底）。"""
            if "INSTR" in r.upper():
                return {"resource": r, "kind": "usb/gpib", "online": bool(idn), "idn": idn}
            return {"resource": r, "kind": "other", "online": None}

        # 串口：**子进程隔离**探测（每口一进程，并行）。
        # 为什么不是本进程线程：驱动层 open 可能永久挂起，线程 join 超时只是
        # "放弃等待"——那个线程仍卡在驱动里持有 VISA 原生状态，之后本进程任何
        # VISA 调用都会让进程直接死亡（2026-09-15 实测，MCP 反复掉线）。
        # 子进程超时可 kill，卡住的句柄随子进程一起消失（见
        # common.discovery.probe_serial_isolated 的完整说明）。
        resources = list_resources()
        non_serial = [r for r in resources if not r.upper().startswith("ASRL")]
        serial_res = [r for r in resources if r.upper().startswith("ASRL")]
        # 非串口：共享单个 ResourceManager + 低并发识别。
        # 实测（2026-09-15）16 线程各自建 RM 会随机 VI_ERROR_INV_OBJECT（VISA 运行时
        # 初始化竞争）→ 整轮 discover 间歇性失败；identify_all 统一收口该问题。
        idns = identify_all(non_serial, timeout_ms=2000, workers=4)
        visa = [make_entry(r, idns.get(r)) for r in non_serial]
        for entry in probe_serials_isolated(serial_res, timeout_ms=500):
            idn = entry.get("idn")
            item = {"resource": entry["resource"], "kind": "serial",
                    "online": bool(idn) if idn else (None if entry.get("note") else False)}
            if idn:
                item["idn"] = idn
            if entry.get("note"):
                item["note"] = entry["note"]
            visa.append(item)

        # LAN 网段扫描：扫**本机所有接口**所在的网段（不只默认路由那张网卡）。
        # 为什么不用单网段（detect_cidr）：本机常挂着多个私网段（公司网/其它 WiFi/
        # 仪器网段/VPN），默认路由指向别的网段时会静默扫错并返回空结果；代理 TUN
        # 开启时出口 IP 还会变成 198.18.0.1 致整个 LAN 段被跳过——而仪器网段的接口
        # 地址其实一直都在（TUN 只加虚拟网卡、不改物理网卡地址）。
        # 任何异常都降级为警告，绝不拖垮 VISA 结果。
        # 由窄到宽：近邻 /24 先扫（~5s），未命中才放宽到真实网段（可能 ~166s）
        segs = local_scan_segments([cidr] if cidr else None)
        lan, warn = {}, None
        try:
            if not segs:
                hint = ("检测到本机在跑代理/VPN，且没有可扫的私网接口地址；"
                        "请把 192.168.0.0/16、10.0.0.0/8、172.16.0.0/12 加入代理直连，"
                        "或用 resource 参数直连。") if proxy_likely() else \
                       "本机没有 RFC1918 私网接口地址；可显式传 cidr 指定网段。"
                warn = f"LAN 部分跳过（未得到可扫网段）。{hint}"
            else:
                # **分两轮扫**：窄段（/24 及更窄）先合并扫，命中任何仪器就停；只有窄段
                # 一无所获时才放宽到宽网段（如 ZeroTier 的 /16），且跳过已扫过的地址。
                #
                # 为什么不是"合并所有网段一次预筛"（2026-09-23 实测改回）：本机
                # ZeroTier 的 172.29.0.0/16 一段就 65534 个地址、预筛 **168.0s** 且
                # 存活 0；而三个 /24 合计仅 **2.0s**，仪器（SDS/SDG/DH1766）全在
                # 192.168.31.0/24。原实现为省"逐段串行"的调度开销把全部网段**合并**成
                # 一次预筛，结果把 /16 也吞了进来 → 整轮 190.2s（其中 87% 是这一段）。
                # `local_scan_segments()` 本来就按"由窄到宽"排序，这里照它的分组走。
                #
                # 注意**不能**改成"跳过虚拟网卡网段"：本机 10.165.75.0/24 也是 ZeroTier，
                # 却有设备响应；远程实验台完全可能挂在 VPN 网段里。宁可多扫一轮，
                # 不可静默漏检（与本文件"真实掩码"那条注释同一个教训）。
                narrow = [s for s in segs
                          if ipaddress.ip_network(s, strict=False).prefixlen >= 24]
                wide = [s for s in segs
                        if ipaddress.ip_network(s, strict=False).prefixlen < 24]
                scanned: set[str] = set()
                for group in (narrow, wide):
                    if not group or lan:
                        continue      # 窄段已命中仪器 → 不再付宽网段的代价
                    per_seg_addrs = {
                        seg: [a for a in (str(h) for h in
                                          ipaddress.ip_network(seg, strict=False).hosts())
                              if a not in scanned]
                        for seg in group
                    }
                    group_addrs = [a for addrs in per_seg_addrs.values() for a in addrs]
                    if not group_addrs:
                        continue
                    # asyncio 并发 + 限流预筛（比线程池逐地址 4 端口串行快 ~7 倍；
                    # 限流是必需的——不限并发会假阴性漏检，见 probe_alive_many 说明）
                    open_ports = probe_open_ports(group_addrs)
                    alive_set = {a for a, ports in open_ports.items() if ports}
                    candidates: list[str] = []
                    for seg, addrs in per_seg_addrs.items():
                        alive = [a for a in addrs if a in alive_set]
                        # 代理 fake-IP 模式会对任意 IP 立即 SYN-ACK → 全部误报 alive →
                        # 后续 VISA open 逐个挂起拖死线程池（实测 254 全 alive 卡死 5min+）
                        if len(alive) > 64:
                            w = (f"网段 {seg} 探测到 {len(alive)}/{len(addrs)} 地址端口全开——"
                                 "疑似代理/VPN fake-IP 干扰，该网段结果已丢弃。"
                                 "请把内网网段加入代理直连（bypass），或用 resource 直连。")
                            warn = f"{warn} {w}" if warn else w
                            continue
                        candidates.extend(alive)
                    scanned |= set(group_addrs)
                    if candidates:
                        # 共享单个 RM（并发各自建 RM 会随机 VI_ERROR_INV_OBJECT，
                        # 且异常从 rm.close() 抛出会冲垮整轮扫描——见 identify_lan_all）
                        for r in identify_lan_all(candidates, timeout_ms=1500,
                                                  open_ports=open_ports).values():
                            if r:
                                lan[r[0]] = r[1]
        except Exception as e:
            w = f"LAN 扫描异常降级（VISA 结果不受影响）: {type(e).__name__}: {e}"
            warn = f"{warn} {w}" if warn else w

        # 把发现到的设备按 IDN 回写地址缓存：之后各专用工具无需显式传 resource
        # 即可自动解析（IP 会变，所以地址只做"上次成功"缓存，不是固定资源配置）。
        pairs: list[tuple[str, str]] = [(res, idn) for res, idn in lan.items()]
        pairs += [(e.get("resource", ""), e.get("idn") or "") for e in visa]
        recognised = _remember_candidates(pairs)

        # DH1766 特例：任何远程会话（含发现时的 *IDN? 探测）都会把电源置为 REM，
        # 现场面板可能因此不可操作；探测到它就补发一次 SYST:LOC 归还面板控制权
        # （仅改面板控制权，不触碰输出/电压/模式；失败静默，不影响发现结果）。
        restored_local = False
        psu_res = recognised.get("psu")
        if psu_res:
            try:
                from dh1766_control import DH1766
                from dh1766_control.visa import VisaClient

                p = DH1766(VisaClient(psu_res, timeout_ms=2000))
                p.local()
                p.client.close()
                restored_local = True
            except Exception:
                restored_local = False

        # cidr 为兼容字段（消费方可能还在读它）：给出"真正扫出设备的网段"，
        # 扫不到设备时退回第一个候选；权威列表在 cidrs。
        hit_segs: list[str] = []
        for res in lan:
            m = re.search(r"::([\d.]+)::", res)
            if not m:
                continue
            try:
                addr = ipaddress.ip_address(m.group(1))
            except ValueError:
                continue
            for seg in segs:
                if addr in ipaddress.ip_network(seg, strict=False) and seg not in hit_segs:
                    hit_segs.append(seg)
        out = {"cidr": (hit_segs[0] if hit_segs else (segs[0] if segs else None)),
               "cidrs": segs, "lan": lan, "visa": visa,
               "resolved": _known_resources(), "recognised_now": recognised,
               "psu_local_restored": restored_local}
        if warn:
            out["warning"] = warn
        return out

    return _call("discovery", lambda: None, fn, close_fn=lambda _: None)


# ============ 故障维护兜底：USB-TMC 卡死恢复 ============
#
# 与 instr_discover 同级的"环境维护"工具，不属于任何设备类。
# 恢复顺序见 AGENTS.md 铁律#14：先重连 → 不行再重启该 USB 的 PnP 设备
# （USB 重新枚举，**仪器固件不重启、设定不丢**；DG832 实测 2.4s 恢复）→ 最后才拔插/断电。

@device_tool()
def usb_reset(kind: str | None = None, resource: str | None = None,
              confirm: bool = False, escalate: bool = False,
              verify_idn: bool = True, timeout_s: int = 20) -> str:
    """⚠ 故障兜底：重启某台仪器所占用的 USB PnP 设备节点（USB-TMC 卡死时用）。

    用在哪：`*IDN?` 超时 / VI_ERROR_TMO / VI_ERROR_SYSTEM_ERROR，且**先重连一次仍
    不恢复**时。效果：USB 重新枚举——**固件不重启、通道设定/输出/保护全部保留**
    （实测 2.4s）；比拔插 USB 省事，**不要**给仪器断电。

    参数：kind（sds/sdg/dmm/dho/mho/dg/psu）或 resource 二选一；confirm=True 必填
    （该仪器所有会话中断 2~3 秒）；verify_idn=True 复位后轮询 *IDN? 确认。
    仅支持 USB 资源——LAN 卡死请先重连/换协议。

    ⚠ 需管理员权限，MCP 进程不能自行提权、不弹 UAC——出路见 skill instrument-mcp
    →「USB-TMC 卡死」。
    """
    if not confirm:
        return _err("confirm_required",
                    "usb_reset 会重启该仪器的 USB 设备节点（会话中断约 2~3 秒），需 confirm=True",
                    "USB")
    if not kind and not resource:
        return _err("param_validation", "需要 kind 或 resource 之一", "USB")

    def fn(_):
        from common.resolver import resolve as _res
        from common.usb_reset import (find_instance, is_admin, parse_usb_resource,
                                      restart_device, wait_back)
        res = resource or _res(kind)
        parsed = parse_usb_resource(res)
        if not parsed:
            raise ValueError(f"不是 USB-TMC 资源串（本工具只处理 USB）：{res}")
        vid, pid, serial = parsed
        inst = find_instance(vid, pid, serial)
        if not inst:
            raise RuntimeError(f"未找到 VID=0x{vid} PID=0x{pid} 的 PnP 设备（设备可能已掉线，需拔插）")
        if not is_admin():
            # 非管理员下 pnputil 必然失败（提权只能由 MCP 之外的进程发起）——
            # 这里直接给可执行的出路，不要让它以一句 pnputil 权限错误收场。
            raise RuntimeError(
                "改动设备节点需要管理员权限，MCP 进程无法自行提权。二选一："
                f'① 在管理员终端执行 pnputil /restart-device "{inst}"；'
                "② 退出 MCP 走 CLI 提权（弹 UAC）："
                f"python common/usb_reset.py --kind <kind> --allow-reset --escalate --verify-idn")
        ok, out = restart_device(inst)
        verified = None
        if ok and verify_idn:
            verified, _note = wait_back(res, timeout_s)
        return {"resource": res, "instance": inst, "restart_ok": ok,
                "output": out[:200], "verified_idn": verified}

    return _call("USB", lambda: None, fn, close_fn=lambda _: None)


# ============ 通用护栏 SCPI（新设备零代码接入） ============
#
# 设计取舍：不做多设备接口统一——各库专用工具承载人工筛选的语义与安全门；
# 通用工具只提供"对照手册直发命令"的护栏通道，设备用出价值后再补专用库。

# 复位/存储覆写类黑名单（AGENTS.md 安全红线）：confirm=True 也不放行——
# 需显式授权的复位场景走测试脚本（如 dh1766 --allow-rst），不经 MCP。
_FORBIDDEN_COMMON_RE = re.compile(r"\*(RST|SAV|RCL)")  # *RST / *SAV n / *RCL n

# 子系统助记符表：(短形式, 长形式)。SCPI 允许短形式与长形式之间的**任意前缀**
# （SCPI-99 §6.2.2 命令助记符），只比对两种写法会漏掉中间缩写
# （实测漏网：`:SYST:RESE`、`:SYST:PRESE`、`:SYST:COMMU:RLST RWL`）。
_RESET_NODES = (("RES", "RESET"), ("FACT", "FACTORY"), ("PRES", "PRESET"))
_LOCK_NODES = (("REM", "REMOTE"), ("RWL", "RWL"), ("LOCK", "LOCKED"))
_COMM_NODES = (("COMM", "COMMUNICATE"),)
_RLST_NODES = (("RLS", "RLSTATE"),)


def _mnemonic(token: str, short: str, long: str) -> bool:
    """SCPI 助记符匹配（宽松，黑名单用「宁可误拦」的偏置）。

    接受三类写法：① short..long 之间的任意前缀（SCPI-99 §6.2.2）；② 长形式本身；
    ③ 短形式开头后粘连参数（如 `SYST:REMON` = `SYST:REM ON`，历史实现按正则前缀
    搜索能拦下，行为必须保持）。
    """
    t, lo = token.upper(), long.upper()
    return len(t) >= len(short) and (lo.startswith(t) or t.startswith(short))


def _any_node(token: str, pairs: tuple[tuple[str, str], ...]) -> bool:
    return any(_mnemonic(token, s, l) for s, l in pairs)


def _classify_forbidden(cmd: str) -> str | None:
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
        if _FORBIDDEN_COMMON_RE.search(stripped):
            return "reset"
        segs = [s for s in head.split(":") if s]
        if not segs or not _mnemonic(segs[0], "SYST", "SYSTEM"):
            continue
        if len(segs) >= 2 and _any_node(segs[1], _RESET_NODES):
            return "reset"
        if len(segs) >= 2 and _any_node(segs[1], _LOCK_NODES):
            return "lock"
        if len(segs) >= 3 and _any_node(segs[1], _COMM_NODES) and _any_node(segs[2], _RLST_NODES):
            return "lock"
    return None


# 单条命令单元的形状：命令头以 `?` 结尾，问号后**允许**带参数。
#
# `?` 后带参数是标准 SCPI 写法（`:MEASure:ITEM? VPP,CHANnel2`、`SAMPle:COUNt? MAX`），
# 故不能按"整段以 ? 结尾"判（2026-09-15 曾因此把带参数查询全拒了，用户报障后修正）。
_QUERY_UNIT_RE = re.compile(r"^[:*]?[A-Za-z][A-Za-z0-9:<>{}_.]*\?(?:\s[\s\S]*)?$")


def _is_query_only(cmd: str) -> bool:
    """整条消息是否**纯查询**：每个 `;` 分段都必须是查询单元（问号后允许带参数）。

    为什么按"分段"而不是"整条"判：SCPI 里 `;` 分隔的是**同一条消息内的多个命令单元**，
    设备会逐个执行——实测 DG832 `:SOUR1:PHAS?;:SOUR1:PHAS 123` 的写单元真的生效
    （Keysight 手册明文：`TRIG:SOUR EXT;COUNT 10` 等价于两条命令）。只查首尾会让写命令
    从查询口溜进去；只允许单条单元又会把**多段回读**（`:CHANnel4:DISPlay?;:CHANnel4:SCALe?`）
    一起拒掉——那是合法且常用的用法（2026-09-15 曾这样过度收紧，用户报障后修回）。

    逐段判用同一条正则（一条命令单元 = `_QUERY_UNIT_RE`），不解析助记符：
    写命令（头里无 `?`）、混合消息（`:OUTP1 ON;:OUTP1?`）、复位/锁定类一律拦。
    """
    units = [u for u in (p.strip() for p in (cmd or "").split(";"))]
    if not units or any(not u for u in units):
        return False
    return all(_QUERY_UNIT_RE.match(u) for u in units)


def _is_forbidden(cmd: str) -> bool:
    """黑名单判定（复位/存储覆写一律拦；远程锁定类**只拦写**）。

    语义：复位/存储覆写类一律 forbidden；远程锁定类只在**非纯查询**时拦——
    `SYST:REM?`、`:SYSTem:LOCKed?` 这类纯查询不改变锁定状态，保留用于状态诊断。
    判定按 `;` 分段做，且接受 SCPI 长短形式之间的任意前缀缩写。
    """
    kind = _classify_forbidden(cmd)
    if kind == "reset":
        return True
    if kind == "lock":
        return not _is_query_only(cmd)
    return False


_ERR_CLEAN_RE = re.compile(r"^\+?0\s*,")


def _drain_errors(c) -> list[str]:
    """排空 SYST:ERR? 队列（铁律2），上限 20 条防死循环。"""
    errs: list[str] = []
    for _ in range(20):
        try:
            r = c.query("SYST:ERR?").strip()
        except Exception as e:
            return errs + [f"(SYST:ERR? 查询失败: {type(e).__name__})"]
        if _ERR_CLEAN_RE.match(r) or "no error" in r.lower():
            return errs
        errs.append(r)
    return errs + ["(错误队列超过 20 条，停止排空)"]


def _visa(resource: str, timeout_ms: int = 5000):
    from common.visa_client import VisaClient
    return VisaClient(resource, timeout_ms=max(500, min(timeout_ms, 30000)))


def _generic_budget(args, kwargs) -> float:
    """通用工具预算：随调用方 `timeout_ms` 走（下限 30s 覆盖 VISA 冷启动）。

    通用工具会指向**任意**发现到的地址（含离线），给它的等待上限要与它自己的
    IO 超时相称——不能像专用工具那样一刀切用默认预算。
    """
    try:
        t = float(kwargs.get("timeout_ms", 5000))
    except (TypeError, ValueError):
        t = 5000.0
    return max(30.0, 12.0 + t / 1000.0)


def _guarded_call(resource: str, timeout_ms: int, fn) -> str:
    """通用工具的同步执行体（预算在 `@device_tool(budget_s=_generic_budget)` 层）。"""
    return _call("instruments", lambda: _visa(resource, timeout_ms), fn,
                 resource=resource)


def _audit_scpi(tool: str, resource: str, cmd: str, **extra) -> str:
    """通用 SCPI 写留痕：TEST_DATA/common/mcp_scpi_audit_YYYYMMDD.jsonl（含拒绝记录）。"""
    d = Path(ROOT) / "TEST_DATA" / "common"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"mcp_scpi_audit_{datetime.now():%Y%m%d}.jsonl"
    entry = {"ts": datetime.now().isoformat(timespec="seconds"),
             "tool": tool, "resource": resource, "cmd": cmd, **extra}
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    return str(p)


@device_tool(budget_s=_generic_budget)
def instr_query(resource: str, cmd: str, timeout_ms: int = 5000) -> str:
    """通用 SCPI 查询——新设备零代码接入。resource 必填（完整 VISA 资源串）；
    cmd 必须含 '?'；timeout_ms 500-30000，默认 5000。只读不留痕；写操作用 instr_write。
    **3458A 不是 SCPI 表，不要用它。**

    护栏：每条 `;` 分段都必须是纯查询（命令头带 '?'，**问号后允许带参数**），
    夹带写/复位/锁定则整条拒。串口(ASRL)按 9600 波特。
    判据细节与看门狗见 skill instrument-mcp →「通用护栏工具」。
    """
    if "?" not in cmd:
        return _err("param_validation", f"查询命令必须含 '?': {cmd!r}",
                    "instruments", resource)
    if _is_forbidden(cmd) or not _is_query_only(cmd):
        _audit_scpi("instr_query", resource, cmd, refused="multi_or_forbidden")
        return _err("forbidden",
                    f"instr_query 只接受纯查询消息：每个 ';' 分段都必须是查询单元"
                    f"（命令头带 '?'，**问号后可带参数**，如 ':MEASure:ITEM? VPP,CHANnel1'），"
                    f"且不得含复位/锁定类命令: {cmd!r}", "instruments", resource)

    def fn(c):
        return {"response": c.query(cmd)}

    return _guarded_call(resource, timeout_ms, fn)


@device_tool(budget_s=_generic_budget)
def instr_write(resource: str, cmd: str, readback_cmd: str | None = None,
                confirm: bool = False, timeout_ms: int = 5000) -> str:
    """通用 SCPI 写——新设备零代码接入。⚠ 必须 confirm=True（raw 写权限大）。
    **3458A 不是 SCPI 表，不要用它。**

    护栏：复位/存储覆写类一律 forbidden（confirm 也不放行）；写前 drain、写后逐条
    SYST:ERR?；readback_cmd 给定即自动回读（同样受黑名单约束）；每次调用含被拒均落盘
    审计；看门狗超时记 executed="unknown"（命令可能已送达，须回读确认）。
    返回 result: {written, pre_errors, syst_errors, readback}。
    细节见 skill instrument-mcp →「通用护栏工具」。
    """
    if _is_forbidden(cmd):
        _audit_scpi("instr_write", resource, cmd, refused="forbidden")
        return _err("forbidden", f"复位/存储覆写类命令禁止经 MCP 下发: {cmd!r}",
                    "instruments", resource)
    if not confirm:
        _audit_scpi("instr_write", resource, cmd, refused="confirm_required")
        return _err("confirm_required", "通用写需 confirm=True（raw SCPI 写权限）",
                    "instruments", resource)
    if readback_cmd and "?" not in readback_cmd:
        return _err("param_validation",
                    f"readback_cmd 是回读查询，必须含 '?': {readback_cmd!r}",
                    "instruments", resource)
    # 回读命令走的是 query()（同样会真的下发）——必须与 cmd 同受黑名单/纯查询约束，
    # 否则 "*RST;*IDN?" 可以从 readback_cmd 溜进去（历史缺陷）。
    if readback_cmd and (_is_forbidden(readback_cmd) or not _is_query_only(readback_cmd)):
        _audit_scpi("instr_write", resource, readback_cmd, refused="readback_multior_forbidden")
        return _err("forbidden",
                    f"readback_cmd 只接受纯查询消息且不得含复位/锁定类命令: {readback_cmd!r}",
                    "instruments", resource)
    _audit_scpi("instr_write", resource, cmd, refused=None, readback_cmd=readback_cmd)

    def fn(c):
        pre = _drain_errors(c)
        c.write(cmd)
        time.sleep(0.1)  # 设备解析入队延迟（串口/低速设备尤其需要）
        post = _drain_errors(c)
        rb = c.query(readback_cmd).strip() if readback_cmd else None
        return {"written": cmd, "pre_errors": pre or None, "syst_errors": post,
                "readback": {"cmd": readback_cmd, "response": rb}
                if readback_cmd else None,
                # 多段回读额外给"段↔值"配对（避免调用方自己数字段、写错顺序不报错）
                "readback_fields": _pair_readback(readback_cmd, rb)}

    result = _guarded_call(resource, timeout_ms, fn)
    outcome = json.loads(result)
    # 看门狗超时只证明"没等到回包"，命令可能已送达并生效——审计必须记 unknown，
    # 否则事后无法区分"没执行"与"执行了没读到"（回读确认是第一手段）。
    _audit_scpi("instr_write", resource, cmd,
                executed="unknown" if outcome.get("error_type") == "timeout" else True,
                outcome=outcome)
    return result


# ============ SDS 示波器 ============

@device_tool()
def sds_status(resource: str | None = None) -> str:
    """SDS 示波器只读快照：IDN/采集/时基/触发/各通道档位耦合。"""
    return _call("SDS", lambda: _sds(resource), lambda s: s.snapshot())


@device_tool(budget_s=300.0)
def sds_auto_scale(ch: int, use_autoset: bool = False, resource: str | None = None) -> str:
    """SDS 自动定标让通道 ch(1-4) 波形正确显示。
    use_autoset=True 为破坏性 :AUToset（重置所有通道档位/时基/触发），仅限
    简单周期信号且无其他已调好通道时显式启用；默认 SCPI 闭环只动目标通道。返回 actions/vpp/freq/adjusted。"""
    return _call("SDS", lambda: _sds(resource),
                 lambda s: s.auto_scale(ch, use_autoset=use_autoset))


@device_tool()
def sds_measure(item: str, ch: int = 4, resource: str | None = None) -> str:
    """SDS 单次测量（SIMPLE 模式，自动切模式 + 设信源）。ch=1-4。
    item 取值（SIMPle:ITEM 表，51 项全支持）**完整枚举**见 skill instrument-mcp
    →「参数语义速查 · sds_measure」。

    无有效读数（如无信号测频率）会在超时后返回 device_error——**属正常现象，非故障**。
    """
    return _call("SDS", lambda: _sds(resource),
                 lambda s: s.measure_simple(item, f"C{ch}"))


@device_tool()
def sds_measure_phase(src_a: str = "C2", src_b: str = "C1",
                      resource: str | None = None) -> str:
    """SDS 双通道相位差（度）= B 相对 A 的相位（A/B 第一个上升沿中值点间）。
    用后自动关闭占用槽并恢复测量模式。用前请确认两通道完整周期在屏内
    （否则无有效值返回 device_error）。2026-09-08 实测：A=C2/B=C1 得 94.664°，
    交换后 265.218°（互补，符号约定验证通过）。"""
    def fn(s: SDS):
        prev_mode = s.meas_mode()
        slot = None
        try:
            r = s.measure_phase(src_a.upper(), src_b.upper())
            slot = r["slot"]
            return r
        finally:
            try:
                if slot is not None:
                    s.adv_slot(slot, False)
                if prev_mode:
                    s.meas_mode(prev_mode)
            except Exception:
                pass
    return _call("SDS", lambda: _sds(resource), fn)


@device_tool()
def sds_meas_threshold(source: str | None = None, thr_type: str | None = None,
                       absolute: str | None = None, percent: str | None = None,
                       resource: str | None = None) -> str:
    """SDS 测量阈值配置/查询（手册 p.183-185）——所有边沿类测量的判据基础。

    source: 阈值源（C1-C4/F1/M1/REF A-D…）；thr_type: PERCent|ABSolute；
    absolute: "high,mid,low" 绝对阈值（V，如 "3,1,-1.5"）；
    percent: "high,mid,low" 百分比（整型，high∈[3,99]/mid∈[2,98]/low∈[1,97]）。
    全部省略 = 查询当前配置。绝对阈值取决于档位/位移/探头系数，设置前先设好。
    """
    def fn(s: SDS):
        out: dict = {}
        if source is not None:
            s.meas_threshold_source(source)
            out["source"] = s.meas_threshold_source()
        if thr_type is not None:
            s.meas_threshold_type(thr_type)
            out["type"] = s.meas_threshold_type()
        if absolute is not None:
            h, m, low = [float(x) for x in absolute.split(",")]
            s.meas_threshold_absolute(h, m, low)
            out["absolute"] = s.meas_threshold_absolute()
        if percent is not None:
            h, m, low = [int(x) for x in percent.split(",")]
            s.meas_threshold_percent(h, m, low)
            out["percent"] = s.meas_threshold_percent()
        if not out:
            out = {
                "source": s.meas_threshold_source(),
                "type": s.meas_threshold_type(),
                "absolute": s.meas_threshold_absolute(),
                "percent": s.meas_threshold_percent(),
            }
        return out
    return _call("SDS", lambda: _sds(resource), fn)


@device_tool()
def sds_meas_gate(on: bool | None = None, ga: float | None = None,
                  gb: float | None = None, resource: str | None = None) -> str:
    """SDS 测量门限（手册 p.179-180）：只统计 GA~GB 窗口内的波形。

    on: 门限开关；ga/gb: 门限位置（秒，相对触发的水平位置，ga≤gb）。
    全部省略 = 查询当前门限开关与位置。
    """
    def fn(s: SDS):
        out: dict = {}
        if on is not None:
            s.meas_gate(on)
            out["gate_on"] = s.meas_gate()
        if ga is not None or gb is not None:
            s.meas_gate_pos(ga, gb)
            out["position"] = s.meas_gate_pos()
        if not out:
            out = {"gate_on": s.meas_gate(), "position": s.meas_gate_pos()}
        return out
    return _call("SDS", lambda: _sds(resource), fn)


@device_tool()
def sds_meas_statistics(on: bool | None = None, max_count: int | None = None,
                        histogram: bool | None = None, reset: bool = False,
                        slot: int | None = None, which: str = "ALL",
                        resource: str | None = None) -> str:
    """SDS 高级测量统计（手册 p.166-174）。

    配置：on=统计开关；max_count=最大统计次数 [0,1024]（0=无限，有限制时
    历史统计才有效）；histogram=直方图开关；reset=True 重置统计结果。
    查询：给 slot（P1-P12）则返回该槽统计，which=ALL|CURRent|MEAN|MAXimum|
    MINimum|STDev|COUNt（ALL 返回完整串）。
    全省略 = 返回统计配置。
    """
    def fn(s: SDS):
        out: dict = {}
        if on is not None:
            s.meas_statistics(on)
            out["statistics_on"] = s.meas_statistics()
        if max_count is not None:
            s.meas_stat_max_count(max_count)
            out["max_count"] = s.meas_stat_max_count()
        if histogram is not None:
            s.meas_stat_histogram(histogram)
            out["histogram"] = s.meas_stat_histogram()
        if reset:
            s.meas_stat_reset()
            out["reset"] = True
        if slot is not None:
            out["slot"] = slot
            out["value"] = s.adv_statistics(slot, which)
        if not out:
            out = {
                "statistics_on": s.meas_statistics(),
                "max_count": s.meas_stat_max_count(),
                "histogram": s.meas_stat_histogram(),
            }
        return out
    return _call("SDS", lambda: _sds(resource), fn)


@device_tool()
def sds_meas_dtime(index: int = 1, edge1: int | None = None,
                   edge2: int | None = None, slope1: str | None = None,
                   slope2: str | None = None, threshold1: float | None = None,
                   threshold2: float | None = None, resource: str | None = None) -> str:
    """SDS 延迟测量（ΔTime）配置/查询（手册 p.176-178）。index=1-4。

    edge1/edge2: 沿序号（-1=最后一个沿）；slope1/slope2: POSitive|NEGative；
    threshold1/threshold2: 沿阈值（V 或百分比，依测量阈值类型）。
    全省略 = 查询该 ΔTime 的六项配置。
    """
    def fn(s: SDS):
        kwargs = {}
        if edge1 is not None:
            kwargs["edge1"] = edge1
        if edge2 is not None:
            kwargs["edge2"] = edge2
        if slope1 is not None:
            kwargs["slope1"] = slope1
        if slope2 is not None:
            kwargs["slope2"] = slope2
        if threshold1 is not None:
            kwargs["threshold1"] = threshold1
        if threshold2 is not None:
            kwargs["threshold2"] = threshold2
        return s.dtime_config(index, **kwargs)
    return _call("SDS", lambda: _sds(resource), fn)


@device_tool()
def sds_meas_display(rdisplay: str | None = None, style: str | None = None,
                     linenumber: int | None = None, strategy: str | None = None,
                     astra_base: str | None = None, astra_top: str | None = None,
                     resource: str | None = None) -> str:
    """SDS 测量显示与幅值策略（手册 p.174-181）。

    rdisplay: 结果显示样式 EMBedded（内嵌压缩波形）|FLOating（悬浮）；
    style: 统计显示模式 M1（垂直，含直方图）|M2（水平）；
    linenumber: M2 模式显示测量项总数 [1,12]；
    strategy: 幅值计算策略 AUTO|MANual；
    astra_base/astra_top: 手动策略的底端/顶端方式 HISTogram|MAX。
    全省略 = 查询当前全部设置。
    """
    def fn(s: SDS):
        out: dict = {}
        if rdisplay is not None:
            s.meas_result_display(rdisplay)
            out["rdisplay"] = s.meas_result_display()
        if style is not None:
            s.adv_style(style)
            out["style"] = s.adv_style()
        if linenumber is not None:
            s.adv_line_number(linenumber)
            out["linenumber"] = s.adv_line_number()
        if strategy is not None:
            s.amp_strategy(strategy)
            out["strategy"] = s.amp_strategy()
        if astra_base is not None or astra_top is not None:
            s.amp_strategy_base_top(astra_base, astra_top)
            out["base_top"] = s.amp_strategy_base_top()
        if not out:
            out = {
                "rdisplay": s.meas_result_display(),
                "style": s.adv_style(),
                "linenumber": s.adv_line_number(),
                "strategy": s.amp_strategy(),
                "base_top": s.amp_strategy_base_top(),
            }
        return out
    return _call("SDS", lambda: _sds(resource), fn)


@device_tool()
def sds_get_waveform(ch: int = 2, points: int = 50000, save_csv: bool = False,
                     resource: str | None = None) -> str:
    """SDS 读取通道波形数据（电压 + 时间轴，经 FFT 交叉验证可信）。ch=1-4；
    points 默认 50000（受设备 ACQ:POIN? 上限约束；过大很慢）。save_csv=True 时存 CSV
    到 TEST_DATA/common/。返回**摘要**（点数/时间窗/Vpp/interval/档位）+ 可选 CSV 路径，
    不返回完整数组（防上下文爆炸）。

    时间轴可信性依据与实测值见 skill instrument-mcp。
    **分析频率请用 FFT/自相关**，朴素过零对调幅信号会误判。
    """
    def fn(s: SDS):
        wf = s.get_waveform(ch, points=points)
        vs = wf["v"]
        out = {
            "ch": ch,
            "points": wf["points"],
            "interval_s": wf["interval_s"],
            "vdiv_v": wf["vdiv_v"],
            "adc_bit": wf["adc_bit"],
            "v_min": min(vs),
            "v_max": max(vs),
            "vpp": max(vs) - min(vs),
            "t_start_s": wf["t"][0],
            "t_end_s": wf["t"][-1],
        }
        if save_csv:
            from datetime import datetime
            p = Path(ROOT) / "TEST_DATA" / "common" / (
                f"mcp_sds_wave_ch{ch}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["time_s", "voltage_v"])
                w.writerows(zip(wf["t"], wf["v"]))
            out["csv"] = str(p)
        return out
    return _call("SDS", lambda: _sds(resource), fn)


@device_tool()
def sds_screenshot(resource: str | None = None) -> str:
    """SDS 截屏并保存 PNG，返回文件路径——**该 PNG 可直接用 Read 工具查看**（AI
    视觉判断波形形态/削顶/居中/菜单状态/光标/测量栏）。2026-09-09 修复
    BMP alpha=0 致全透明问题（存前 convert('RGB')）。

    用途：设备测量值超屏时被钳制在屏界不可信，截图是"有无波形/是否削顶"的
    物理真相；也可核对面板菜单/光标/测量栏等 SCPI 不便读取的信息。
    无视觉能力时用库的 analyze_screen() 做像素分析兜底。"""
    def fn(s: SDS):
        from datetime import datetime
        p = Path(ROOT) / "TEST_DATA" / "common" / (
            f"mcp_sds_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png")
        saved = s.screenshot_png(p)
        return {"png": str(saved), "hint": "直接 Read 该 PNG 即可看图"}
    return _call("SDS", lambda: _sds(resource), fn)


@device_tool()
def sds_diagnose(resource: str | None = None) -> str:
    """SDS 触发链路诊断：模式/状态/源/电平/时基（无波形时第一步）。"""
    return _call("SDS", lambda: _sds(resource), lambda s: s.diagnose_trigger())


@device_tool()
def sds_shutdown(confirm: bool, resource: str | None = None) -> str:
    """SDS 远程关机。⚠ 破坏性：设备离线需面板手动开机。必须 confirm=True。"""
    if not confirm:
        return _err("confirm_required", "关机需 confirm=True（设备将离线，需手动开机）", "SDS")

    def fn(s: SDS):
        # 设备可能立即断开导致 write 抛 VisaIOError，但关机已生效——区分记录
        try:
            s.shutdown(confirm=True)
            return {"sent": True, "delivered": True, "note": "命令已确认送达"}
        except Exception as e:
            # 断开类异常（写后连接断）视为已送达；其他异常标记未确认
            return {"sent": True, "delivered": False,
                    "note": f"命令已发送但结果未确认: {type(e).__name__}"}

    # 关闭走 _call 默认路径（finally 已 try/except 兜底，关机后断连的 close 异常被吞）
    return _call("SDS", lambda: _sds(resource), fn)


# ============ SDG 信号源 ============

@device_tool()
def sdg_status(resource: str | None = None) -> str:
    """SDG 信号源快照：输出状态/波形参数/调制（两通道）。"""
    return _call("SDG", lambda: _sdg(resource), lambda g: g.snapshot())


@device_tool()
def sdg_set_wave(ch: int, wvtp: str, freq_hz: float, amp_v: float,
                 offset_v: float = 0.0, resource: str | None = None) -> str:
    """SDG 设置通道 ch(1-2) 波形参数。wvtp: SINE/SQUARE/RAMP/PULSE/NOISE/DC；
    freq_hz 单位 Hz；amp_v 单位 V（高阻负载下即 Vpp）；offset_v 单位 V。
    注意：不改变输出开关状态；输出开启时参数实时生效。返回含设备回读。"""
    def fn(g: SDG):
        r = g.set_basic_wave(
            ch, WVTP=wvtp.upper(), FRQ=f"{freq_hz:g}HZ",
            AMP=f"{amp_v:g}V", OFST=f"{offset_v:g}V",
        )
        time.sleep(0.2)
        err = g.system_error()
        return {"set": True, "error": err, "readback": g.basic_wave(ch)}
    return _call("SDG", lambda: _sdg(resource), fn)


@device_tool()
def sdg_counter(on: bool | None = None, resource: str | None = None) -> str:
    """SDG 内置频率计（FCNT，手册 §3.24）。on=None 仅查询；True/False 先开关再查。

    返回 STATE/FRQ/PW/NW/DUTY/FRQDEV/REFQ/TRG/MODE/HFR/TYPE。
    ⚠ 命令集因系列而异：SDG2000X 用 FCNT（本机实测），SDG7000A 才用
    `:SENSe:COUNTer:*`。输入口无信号时 FRQ=0HZ（正常）。
    """
    return _call("SDG", lambda: _sdg(resource), lambda g: g.counter(on))


@device_tool()
def sdg_output(ch: int, on: bool, expect_load: str, confirm: bool = False,
               resource: str | None = None) -> str:
    """SDG 开关通道 ch(1-2) 输出。⚠ 开/关都需 confirm=True（关闭可能打断
    正在进行的测试或他人实验，同样是状态变更）。

    **expect_load 必填**：调用方声明的当前负载设置（HZ=高阻 / 50=50Ω），
    仅校验不设置——与实际不符立即拒绝并回传实际值。SDG 的 AMP 设定与负载
    强相关（HZ 下即 Vpp，50Ω 下实际幅度减半），输出前必须声明避免误判。
    建议先 sdg_status 查看。
    """
    if not confirm:
        return _err("confirm_required",
                    f"输出开关（{'ON' if on else 'OFF'}）需 confirm=True——"
                    f"关闭同样可能打断正在进行的测试/实验", "SDG")

    def fn(g: SDG):
        g.set_output(ch, on, expect_load)
        time.sleep(0.3)
        return g.output_state(ch)

    return _call("SDG", lambda: _sdg(resource), fn)


# ============ Keysight 34465A ============

@device_tool()
def dmm_measure(function: str, resource: str | None = None) -> str:
    """34465A 单次测量。function: volt_dc/volt_ac/curr_dc/curr_ac/res/fres/
    cont/cap/diod/freq。"""
    return _call("DMM", lambda: _dmm(resource), lambda d: d.measure(function))


@device_tool()
def dmm_status(resource: str | None = None) -> str:
    """34465A 快照：IDN/选件/配置/最近读数。"""
    return _call("DMM", lambda: _dmm(resource), lambda d: d.snapshot())


@device_tool()
def dmm_configure(function: str, range_v: float | None = None,
                  resolution: float | None = None,
                  resource: str | None = None) -> str:
    """34465A 配置测量功能/量程/分辨率（不触发测量）。function: volt_dc/
    volt_ac/curr_dc/curr_ac/res/fres/cap/freq；range_v/resolution 可选。
    注意 :CONF? 回读有滞后一拍特性，以实测值为准。"""
    return _call("DMM", lambda: _dmm(resource),
                 lambda d: (d.configure(function, range_v, resolution),
                            d.configuration())[1])


@device_tool()
def dmm_nplc(value: float | None = None, resource: str | None = None) -> str:
    """34465A 电压 DC 积分时间 NPLC（手册 [SENSe:]VOLTage[:DC]:NPLC）。

    value=None 查询；给出则设置后回读。取值 0.02/0.2/1/10/100（默认 10）——
    越大越准越慢。注意：NPLC 与 APERture 互斥（设孔径会把 NPLC 置 0）。
    """
    def fn(d):
        if value is None:
            return {"nplc": d.get_nplc()}
        d.set_nplc(value)
        time.sleep(0.2)
        return {"nplc": d.get_nplc()}
    return _call("DMM", lambda: _dmm(resource), fn)


# ============ HP/Keysight 3458A 八位半万用表（专用库，**非 SCPI**）============
#
# 3458A 不吃标准 SCPI：没有 *IDN?（用 ID?）、没有 SYST:ERR?（用 ERRSTR?）、复位是
# RESET（不是 *RST）、读数是 `TARM SGL,1` 触发后直接回值、串尾必须 LF、档位/NPLC
# 用 `DCV <range>` / `NPLC <n>` 且**没有回读命令**。因此它不套 common/visa_client.py
# 的 SCPI 假设，也不该走 instr_query/instr_write 通用护栏（那两条面向 SCPI）。
# 库/命令白名单/已知坑见 keysight_3458a/ 与 docs/3458a_integration_20260922.md。


@device_tool()
def ks3458a_status(resource: str | None = None) -> str:
    """3458A 只读状态：`ID?` / `ERRSTR?` / `TEMP?`，外加 `device`（设备回读 =
    真实配置）与 `tracked`（本会话下发过什么）——两者语义分开，别混。

    ⚠ 连接时会做**会话恢复**（IFC/Device Clear + 有限 drain + TARM/TRIG HOLD）
    + **读前准备**（END ALWAYS/INBUF ON/TRIG AUTO）：上次会话若把表留在 free-run
    或 TRIG HOLD，不处理就会读到错位数据 / 超时。代价是可能打断同总线他人采集。

    `device` 字段清单与码表解码见 skill instrument-mcp →「参数语义速查 · ks3458a_status」。
    """
    def fn(d: DMM3458A):
        out = {
            "idn": d.idn(),
            **_ks3458a_error(d),
            "tracked": {"dcv_range": d.current_range, "nplc": d.current_nplc},
            "tracked_note": "本会话已下发过的档位/NPLC；与 device 回读分开报告",
        }
        try:
            st = d.state()
            out["device"] = {k: v for k, v in st.items()
                             if k not in ("tracked", "temperature_c", "error")}
        except Exception as e:                      # noqa: BLE001 —— 回读失败不影响其余
            out["device_error"] = f"{type(e).__name__}: {e}"
        try:
            out["temperature"] = d.temperature()
        except Exception as e:                      # noqa: BLE001 —— 温度取不到不影响其余
            out["temperature_error"] = f"{type(e).__name__}: {e}"
        return out
    return _call("3458A", lambda: _ks3458a(resource), fn)


@device_tool()
def ks3458a_read(resource: str | None = None) -> str:
    """3458A 单次直流电压读数（V）：`TARM SGL,1` 触发一次并回值（命令**不带问号**）。

    只读、**不改配置**：用设备**当前**的档位/NPLC/功能。要指定档位先 `ks3458a_configure`。
    读数非数值时按 device_error 报（**不返回 0 兜底**——错位数据比报错危险）。

    ⚠ 连接时会做会话恢复（见 `ks3458a_status`），共享实验台上可能打断别人的 GPIB 采集。
    """
    return _call("3458A", lambda: _ks3458a(resource), lambda d: {"volts": d.read_dcv()})


@device_tool()
def ks3458a_read_avg(n: int = 10, resource: str | None = None) -> str:
    """3458A 连续读 n 次取平均（降噪）。n 取 1~1000（本工具上限，防误传超大值）。

    逐次 `TARM SGL,1`，耗时随 n 线性增长（10 PLC 约 0.2 s/次 @50Hz）——
    调用墙钟上限 150 s，n 大时请自己算好；不够就分批读。
    """
    if not 1 <= int(n) <= 1000:
        return _err("param_validation", f"n 需在 1~1000（收到 {n!r}）", "3458A")
    return _call("3458A", lambda: _ks3458a(resource),
                 lambda d: {"n": int(n), "volts": d.read_avg(n)})


@device_tool(budget_s=600.0)   # 连续测量：n 点 ×（读数 + 点间间隔），预算给宽些
def ks3458a_read_series(n: int = 10, interval_s: float | None = None,
                        save_csv: bool = False, resource: str | None = None) -> str:
    """3458A **一次会话内连续重复测量 n 次**（批量采集主用），n 取 1~1000。

    与 read_avg/read_stats 的区别：返回**逐点序列**（可选落 CSV）与时长统计；比连续调
    n 次 ks3458a_read 省掉每次 ~0.27 s 重连（100 点 @NPLC 10：~70 s → ~45 s）。
    `interval_s` 是**点间间隔**（主机 sleep，非精密时序）；要精密等间隔采样用
    ks3458a_burst 的 TIMER。

    返回 `{n, values?/summary, csv?}`：values 仅在 n≤50 时内联，更大只回 summary
    （防上下文爆炸）——要逐点数据用 save_csv=True。
    """
    count = int(n)
    if not 1 <= count <= 1000:
        return _err("param_validation", f"n 需在 1~1000（收到 {n!r}）", "3458A")

    def fn(d: DMM3458A):
        res = d.read_series(count, interval_s=interval_s)
        out: dict = {"summary": res["summary"]}
        if count <= 50:
            out["values"] = res["values"]
        if save_csv:
            p = Path(ROOT) / "TEST_DATA" / "ks3458a" / (
                f"mcp_ks3458a_series_n{count}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["index", "volts", "unix_time"])
                writer.writerows((i, v, ts) for i, (v, ts)
                                 in enumerate(zip(res["values"], res["timestamps"])))
            out["csv"] = str(p)
        return out
    return _call("3458A", lambda: _ks3458a(resource), fn)


@device_tool()
def ks3458a_read_stats(n: int = 10, resource: str | None = None) -> str:
    """3458A 连续读 n 次并给统计：`{n, mean, stddev, min, max}`（单位 V）。

    n 取 1~1000；stddev 是**样本标准差**（n-1 分母），n=1 时记 0.0。
    单次读数抖动明显时用这个而不是 `ks3458a_read`（结论看 mean ± stddev）。
    """
    if not 1 <= int(n) <= 1000:
        return _err("param_validation", f"n 需在 1~1000（收到 {n!r}）", "3458A")

    def fn(d: DMM3458A):
        out = d.read_stats(n)
        out["unit"] = "V"
        return out
    return _call("3458A", lambda: _ks3458a(resource), fn)


@device_tool(budget_s=300.0)   # 高速突发：n 大时读块远超默认预算
def ks3458a_burst(n: int = 1000, sample_interval_s: float | None = None,
                dcv_range: float | None = None, data_format: str = "SINT",
                save_csv: bool = False, resource: str | None = None) -> str:
    """3458A 高速二进制突发（100k rdg/s）：一次触发取 n 个读数，返回**摘要**；
    逐点数据用 save_csv=True。n 上限 16777215（越大越慢、内存越大）。

    data_format="SINT"（默认，2 字节/读数）或 "DINT"（4 字节/读数）——
    **信号可能超过档位 120% 时必须用 DINT**（SINT 满量程仅约该档位 120%，会溢出）。

    ⚠ **会改设备配置**：`PRESET DIG` 把整组采样参数复位到数字档。跑完本工具自动收尾并
    `PRESET NORM` 恢复 ASCII 输出（**不是** RESET）；要还原自己的档位/NPLC 请显式
    ks3458a_configure。sample_interval_s / dcv_range 省略则不下发（用设备当前值）。

    完整配方与收尾契约见 skill instrument-mcp →「参数语义速查 · ks3458a_burst」。
    """
    def fn(d: DMM3458A):
        burst = d.read_burst(n, sample_interval_s=sample_interval_s, dcv_range=dcv_range,
                             data_format=data_format)
        out = {"values_count": len(burst["values"]), "summary": burst["summary"]}
        if save_csv:
            p = Path(ROOT) / "TEST_DATA" / "ks3458a" / (
                f"mcp_ks3458a_burst_{str(data_format).upper()}_n{int(n)}_"
                f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["index", "volts"])
                writer.writerows(enumerate(burst["values"]))
            out["csv"] = str(p)
        return out
    return _call("3458A", lambda: _ks3458a(resource, timeout_s=120.0), fn)


@device_tool()
def ks3458a_configure(dcv_range: str | float = 10.0, nplc: float = 10.0,
                    resource: str | None = None) -> str:
    """3458A 设置直流档位与积分时间（`DCV <range>` / `NPLC <n>`）。**改配置**。

    dcv_range 传数值（0.1/1/10/100/1000）→ **固定档**；传 "AUTO" → **自动挡**。
    只动自动挡开关请用 ks3458a_autorange(on)（不碰档位/NPLC）。
    10 V 档物理可到 12 V（120%），但本库选档按 1.1 倍余量（保守）——确知 ≤12 V 又要用
    10 V 档就直接传 10。

    **换档/换配置后库内会自动丢弃第一次读数**（建立时间 + 自校准），有意行为。
    写后用 ks3458a_status 的 `device` 回读复核；**自动挡状态只能看 `ARANGE?`**。

    细节见 skill instrument-mcp →「参数语义速查 · ks3458a_configure」。
    """
    def fn(d: DMM3458A):
        d.configure_dcv(dcv_range, nplc)
        st = {}
        try:
            st = {k: v for k, v in d.state().items()
                  if k in ("function", "range_v", "arange", "autorange", "nplc")}
        except Exception as e:                        # noqa: BLE001 —— 回读失败不影响写结果
            st = {"readback_error": f"{type(e).__name__}: {e}"}
        return {"dcv_range": d.current_range, "nplc": d.current_nplc,
                "device_readback": st,
                **_ks3458a_error(d),
                "note": "device_readback 是 FUNC?/RANGE?/ARANGE?/NPLC? 实测回读；"
                        "errstr 是接受证据"}
    return _call("3458A", lambda: _ks3458a(resource), fn)


@device_tool()
def ks3458a_autorange(on: bool = True, resource: str | None = None) -> str:
    """3458A 自动挡开关（`ARANGE ON` / `ARANGE OFF`，手册 p.160-161）。**改配置**。

    与 `ks3458a_configure(dcv_range="AUTO")` 的区别：这条**只动自动挡开关**，
    不改档位数值与 NPLC。（手册 p.53-54 还提到 `ARANGE ONCE`：让自动挡选一次档位，
    需要时用 `instr_write` 显式下发白名单外的该命令——本工具不封装。）
    """
    def fn(d: DMM3458A):
        d.set_autorange(bool(on))
        st = {}
        try:
            st = {k: v for k, v in d.state().items()
                  if k in ("arange", "autorange", "range_v", "function")}
        except Exception as e:                        # noqa: BLE001
            st = {"readback_error": f"{type(e).__name__}: {e}"}
        return {"requested_autorange": bool(on), "device_readback": st,
                **_ks3458a_error(d)}
    return _call("3458A", lambda: _ks3458a(resource), fn)


@device_tool()
def ks3458a_acv(range: float = 10.0, band_lo: float | None = None,
              band_hi: float | None = None, sync: bool = False,
              nplc: float | None = None, resource: str | None = None) -> str:
    """3458A 交流电压配置（`ACV` / `SETACV` / `ACBAND`，可选 `NPLC`）。**改配置**。

    sync=False → `SETACV ANA`（模拟转换，默认）；True → `SETACV SYNC`（同步采样）。
    band_lo / band_hi 给定时下发 `ACBAND <lo>,<hi>`（Hz，**必须成对给**）；
    nplc 给定时下发 `NPLC <n>`。

    ⚠ **SYNC 路径未实测**；`ANA` 下 `TARM SGL,1` 已实测能读出交流电压（2026-09-23）。
    交流**读数**接口本服务器暂未暴露（库里有 `DMM3458A.read_acv()`）。
    详见 skill instrument-mcp。
    """
    def fn(d: DMM3458A):
        d.configure_acv(range, band_lo=band_lo, band_hi=band_hi, sync=sync)
        if nplc is not None:
            d.set_nplc(nplc)
        return {"acv_range": range, "band_lo": band_lo, "band_hi": band_hi,
                "sync": bool(sync), "nplc": d.current_nplc,
                **_ks3458a_error(d),
                "unverified": "ACV/SETACV/ACBAND 未在参考实现/官方样例中出现，待手册核对"}
    return _call("3458A", lambda: _ks3458a(resource), fn)


@device_tool()
def ks3458a_reset(confirm: bool = False, resource: str | None = None) -> str:
    """3458A 复位：`RESET` + `END ALWAYS` + `INBUF ON`。

    ⚠ **破坏性**：回到**开机测量配置**——档位/NPLC/功能/内存/触发等一并复位。
    必须 confirm=True。

    这是本库**唯一**的重置入口：MCP 连接默认 `reset_on_open=False`，**不会**每次调用
    偷偷复位仪表。`END ALWAYS`/`INBUF ON` 是 `TARM SGL` 单次读数配方的前置条件——
    缺了会把 GPIB 总线占住。完整影响范围见 skill instrument-mcp。
    """
    if not confirm:
        return _err("confirm_required",
                    "复位会回到开机测量配置（档位/NPLC/功能/触发全变），需 confirm=True",
                    "3458A")

    def fn(d: DMM3458A):
        d.reset()
        return {"reset": True, **_ks3458a_error(d),
                "note": "RESET 后已补 END ALWAYS + INBUF ON（TARM SGL 配方前置条件）"}
    return _call("3458A", lambda: _ks3458a(resource), fn)


@device_tool(budget_s=180.0)
def ks3458a_unstick(confirm: bool = False, resource: str | None = None) -> str:
    """3458A **救砖**：`IFC` → clear → `TARM/TRIG HOLD` → `RESET` → clear →
    `END ALWAYS`/`INBUF ON` → 读 `ID?`。

    用于"表能听但一直吐数"（free-run）或全部调用超时、普通 Device Clear 停不下来时。

    ⚠ **对 Talk Only 模式的有效性待验证**：手册只给面板处置（`Address` → `9` → `Enter`
    或按 Reset 键），原理上表只听不讲时 IFC/RESET 送不进去；但 2026-09-23 那次"拔插
    适配器"与"面板按键"同时做过、**成因未能分离**，不能断言远程一定无效。先看/问面板
    `TALK` 指示灯并用 `preflight --id` 分流，两条路都要试，**别只试一条**。

    ⚠ **破坏性**：`RESET` 回到**开机测量配置**（档位/NPLC/功能/触发）→ 必须
    confirm=True。返回体含 `idn`（救回来是 `HP3458A`）与一次电压读数。

    分流判据见 skill instrument-mcp。
    """
    if not confirm:
        return _err("confirm_required",
                    "救砖会下发 RESET（回到开机测量配置：档位/NPLC/功能/触发全变），"
                    "需 confirm=True", "3458A")

    def fn(d: DMM3458A):
        out = d.unstick()
        out["unit"] = "V"
        return out
    return _call("3458A", lambda: _ks3458a(resource, timeout_s=60.0), fn)


# ============ DHO 示波器 ============

@device_tool()
def dho_status(resource: str | None = None) -> str:
    """DHO 示波器只读快照（通道/时基/触发/采集）。"""
    return _call("DHO", lambda: _dho(resource), lambda s: s.snapshot())


@device_tool()
def dho_measure_item(item: str, ch: int = 1, ch2: int | None = None,
                     samples: int = 1, rails: bool = False,
                     resource: str | None = None) -> str:
    """DHO 测量查询。item 枚举（RIGOL 表）: VPP/VMAX/VMIN/VAMP/VAVG/VRMS/
    PERiod/FREQuency/PWIDth/NWIDth/PDUTy/RTIMe/FTIMe 等；ch=1-4；
    双信源项（延迟/相位）需给 ch2。
    `samples>1` 时主机侧连读 N 次返回 mean/min/max/stddev（单次读数抖动大，用均值下结论）；
    `rails=True` 额外读**顶轨/底轨**（VTOP/VBASe）并**上下分别**判断贴边
    （`top_touching`/`bottom_touching`/`edges_touching` + `hints`）；
    无有效读数时分类报因（suspicious/hint：channel_off / off_screen / near_edge /
    few_edges / no_signal；near_edge 另给 `edges_touching`/`edge_hints` 分顶/底）。
    ⚠ **DHO 不在本实验台**：读路径来自共享内核（离线验证 + 手册），写路径未实机验证；
    离屏判据依赖"垂直格数/中心=−offset"标定，DHO 未标定 → 窗口/贴边类判据返回 None。"""
    def fn(s: DHO):
        if samples is None or int(samples) <= 1:
            try:
                val = s.measure_item(item, ch, ch2)
            except ValueError as e:
                try:
                    diag = s.diagnose_no_reading(item, ch)
                except Exception as de:
                    diag = {"diagnosis_error": f"{type(de).__name__}: {de}"}
                raise ToolDiagnosis("device_error", str(e), diag)
            out = {"item": item, "value": val, "ch": ch, "probe_x": s.channel_probe(ch)}
            if rails:
                out["rails"] = s.rails(ch)
            return out
        st = s.measure_stats(item, ch, ch2, samples=int(samples))
        st["ch"] = ch
        return st
    return _call("DHO", lambda: _dho(resource), fn)


@device_tool()
def dho_channel(ch: int, scale: float | None = None, offset: float | None = None,
                coupling: str | None = None, probe: float | None = None,
                display: bool | None = None, resource: str | None = None) -> str:
    """DHO 通道垂直参数设置（档位/偏置/耦合/探头比/显示）——**写后回读验证**。

    与 mho_channel 同一套内核语义：中心 = −offset、固定 **scale → offset** 顺序、
    通道 OFF 时写垂直参数会自动先开（OFF 下写入被静默忽略）、偏置被钳制时 adjusted + reasons。
    ⚠ **DHO 不在本实验台**：本工具为共享内核的离线验证 + 手册核对，未实机验证。"""
    def fn(s: DHO):
        return s.configure_channel(ch, scale=scale, offset=offset, coupling=coupling,
                                   probe=probe, display=display)
    return _call("DHO", lambda: _dho(resource), fn)


@device_tool()
def dho_timebase(scale: float | None = None, offset: float | None = None,
                 resource: str | None = None) -> str:
    """DHO 水平时基设置（s/div 与水平位移），两者都回读。
    ⚠ 时基是**全局**的（影响所有通道）；**DHO 不在本实验台**，未实机验证。"""
    def fn(s: DHO):
        return s.configure_timebase(scale=scale, offset=offset)
    return _call("DHO", lambda: _dho(resource), fn)


@device_tool()
def dho_trigger(source: int | None = None, level: float | None = None,
                slope: str | None = None, mode: str | None = None,
                sweep: str | None = None, resource: str | None = None) -> str:
    """DHO 触发设置（边沿源/电平/斜率、触发模式/扫描）——写后回读，枚举对照手册校验。
    mode ∈ DHO 手册 3.27.1 的 17 项（**CAN/LIN 仅 DHO900**）；slope 第三态 RFALl；
    ⚠ 触发模式/扫描是**全局**设定；**DHO 不在本实验台**，未实机验证。"""
    def fn(s: DHO):
        return s.configure_trigger(source=source, level=level, slope=slope,
                                   mode=mode, sweep=sweep)
    return _call("DHO", lambda: _dho(resource), fn)


# ============ MHO 示波器（RIGOL MHO900 系列，实测基准 MHO984D） ============

@device_tool()
def mho_status(resource: str | None = None) -> str:
    """MHO 示波器只读快照：IDN/SCPI 版本/触发状态与类型/采集方式/存储深度/采样率/
    时基/边沿触发源与电平/CH1-4 开关·耦合·档位·偏移·探头比。
    注意本系列采样率随开启通道数下降（1~2ch 4GSa/s、3~4ch 1GSa/s）。"""
    return _call("MHO", lambda: _mho(resource), lambda s: s.snapshot())


@device_tool()
def mho_measure_item(item: str, ch: int = 1, ch2: int | None = None,
                     samples: int = 1, rails: bool = False,
                     resource: str | None = None) -> str:
    """MHO 测量查询。ch=1-4；双信源项（延迟/相位）需同时给 ch2。
    item 取值：单信源 30 项、双信源 8 项——**完整枚举**见 skill instrument-mcp
    →「参数语义速查 · mho_measure_item」。

    samples>1 连读 N 次（≤200）返回 mean/min/max/stddev/count/invalid——单次读数抖动
    明显，**结论要用均值**；无效读数单独计数。
    rails=True 额外读顶轨/底轨（VTOP/VBASe）并**上下分别**判断贴边：顶贴→offset 调更负、
    底贴→offset 调更大（方向相反故分开报）。

    无有效读数时分类报因（channel_off / off_screen / near_edge / few_edges /
    no_signal，附 hint/window/evidence）：**near_edge 可能是部分削顶后的"看着合理的
    假值"，必须换档**。探头比 ≠1 时幅度类读数为探头端电压（频率不受影响）。
    ⚠ 形态判断用 mho_screenshot 看图（设备读数在超屏时不可信）。
    """
    def fn(s: MHO):
        warnings = list(s._probe_warning(ch))
        if samples is None or int(samples) <= 1:
            try:
                val = s.measure_item(item, ch, ch2)
            except ValueError as e:
                try:
                    diag = s.diagnose_no_reading(item, ch)
                except Exception as de:  # 诊断本身失败不能掩盖原错误
                    diag = {"diagnosis_error": f"{type(de).__name__}: {de}"}
                raise ToolDiagnosis("device_error", str(e), diag)
            out = {"item": item, "value": val, "ch": ch,
                   "probe_x": s.channel_probe(ch)}
            hw = s.horizontal_window()
            if item.strip().upper().startswith(("FREQ", "PER")) and hw and val:
                cycles = hw["span_s"] * (val if item.strip().upper().startswith("FREQ")
                                         else 1.0 / val)
                if cycles < 2:
                    warnings.append(
                        f"屏内约 {cycles:.2f} 个周期（时间窗 {hw['span_s']:.3g} s，"
                        f"{hw['note']}）：读数可能不可靠，建议放宽时基")
            if rails:
                out["rails"] = s.rails(ch)
                if out["rails"].get("edges_touching"):
                    warnings.append("顶/底轨有贴边（见 rails.edges_touching）——"
                                    "幅度类读数可能被削顶，先按 edge_hints 换档再下结论")
            if warnings:
                out["warnings"] = warnings
            return out
        st = s.measure_stats(item, ch, ch2, samples=int(samples))
        st["ch"] = ch
        if warnings:
            st["warnings"] = warnings
        return st
    return _call("MHO", lambda: _mho(resource), fn)


@device_tool()
def mho_channel(ch: int, scale: float | None = None, offset: float | None = None,
                coupling: str | None = None, probe: float | None = None,
                display: bool | None = None, resource: str | None = None) -> str:
    """MHO 通道垂直参数设置（档位/偏置/耦合/探头比/显示）——**写后回读验证**。
    **只影响指定通道**（多信号实验台上不动别人的通道）。

    三条设备行为已在工具内处理：① **屏幕中心电压 = −offset**（按 +offset 理解会把波形
    顶出屏幕）；② 改 scale 会**等比缩放 offset** → 固定按 **scale → offset** 顺序写；
    ③ 通道 OFF 时写 SCALe/OFFSet 被**静默忽略** → 需要写垂直参数时自动先开通道
    （reasons 里说明），display=False 在参数写完后才关。
    偏置有硬件量程上限（**随档位变**）：被钳制时 `adjusted` 非空 + reasons 说明。

    返回 {requested, before, actual, adjusted, reasons, window=[bottom,top]}。
    量程阶梯见 skill instrument-mcp →「参数语义速查 · mho_channel」。
    """
    def fn(s: MHO):
        return s.configure_channel(ch, scale=scale, offset=offset, coupling=coupling,
                                   probe=probe, display=display)
    return _call("MHO", lambda: _mho(resource), fn)


@device_tool()
def mho_timebase(scale: float | None = None, offset: float | None = None,
                 resource: str | None = None) -> str:
    """MHO 水平时基设置（s/div 与水平位移）——**两者都回读**（只回读被写的那个
    等于没回读）。

    ⚠ 时基是**全局**的：影响所有通道的显示与"屏内几个周期"，共享实验台上会改变
    别人的观察。**屏内不足 2 个周期时频率类测量读不到**（是窗口不够，不是精度问题）。

    返回 {requested, before, actual, adjusted, reasons, window_t}。
    """
    def fn(s: MHO):
        return s.configure_timebase(scale=scale, offset=offset)
    return _call("MHO", lambda: _mho(resource), fn)


@device_tool()
def mho_trigger(source: int | None = None, level: float | None = None,
                slope: str | None = None, mode: str | None = None,
                sweep: str | None = None, resource: str | None = None) -> str:
    """MHO 触发设置（边沿源/电平/斜率、模式/扫描）——写后回读，枚举对照手册校验。

    slope ∈ POSitive/NEGative/RFALl；sweep ∈ AUTO/NORMal/SINGle；
    mode ∈ 手册 3.27.1 的 20 项（EDGE/PULSe/…/IIS/FLEXray/M1553）。
    下发顺序 mode → sweep → 源/斜率/电平。
    ⚠ 触发模式/扫描是**全局**设定，共享实验台上会影响他人观察。
    返回 {requested, before, actual, adjusted, reasons}；电平受限时 reasons 里带该
    通道窗口范围。
    """
    def fn(s: MHO):
        return s.configure_trigger(source=source, level=level, slope=slope,
                                   mode=mode, sweep=sweep)
    return _call("MHO", lambda: _mho(resource), fn)


@device_tool(budget_s=300.0)   # 逐档放大搜索最多 12 轮，每轮若干次测量
def mho_fit_channel(ch: int, occupancy: float = 0.7, margin: float = 0.08,
                    max_iter: int = 12, resource: str | None = None) -> str:
    """MHO **单通道**垂直自动定标/居中——只动该通道的 scale/offset（**不是全局 autoset**）。

    判据：① 可测（读数非 9.9E37）② 不贴边（margin 带内）③ 占屏率 ∈ [0.4, 0.9]
    （目标 occupancy，默认 0.7）。
    平直/无信号 → 判 `flat`：**保持档位**、按均值居中（不猜档位，猜了就是假精度）；
    超出量程/未收敛 → ok=false + reason + trace 证据，**不假装成功**。

    返回 before/after/measured/occupancy/margin_ok/adjusted/reasons/trace/warnings。
    算法与设计依据见 skill instrument-mcp →「参数语义速查 · mho_fit_channel」。
    """
    def fn(s: MHO):
        return s.fit_channel(ch, occupancy=occupancy, margin=margin, max_iter=max_iter)
    return _call("MHO", lambda: _mho(resource), fn)


@device_tool()
def mho_screenshot(resource: str | None = None) -> str:
    """MHO 截屏并保存 PNG，返回文件路径——**该 PNG 可直接用 Read 工具查看**
    （波形形态/有无信号/削顶/居中/菜单/测量栏/网络配置）。

    RIGOL 原生回 PNG 位图流（`:DISPlay:DATA? PNG`），无需像 SDS 那样解析 BMP。
    设备测量值超屏时被钳制不可信，截图才是物理真相（AGENTS.md 铁律#9）。"""
    def fn(s: MHO):
        p = Path(ROOT) / "TEST_DATA" / "mho" / (
            f"mcp_mho_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png")
        saved = s.screenshot_png(p)
        return {"png": str(saved), "hint": "直接 Read 该 PNG 即可看图"}
    return _call("MHO", lambda: _mho(resource), fn)


@device_tool()
def mho_get_waveform(ch: int = 1, points: int = 1000, mode: str = "NORMal",
                     fmt: str = "BYTE", save_csv: bool = False,
                     resource: str | None = None) -> str:
    """MHO 读取通道波形（电压 + 时间轴），返回**摘要** + 可选 CSV 路径。

    mode: NORMal=屏幕波形（**1~1000 点**）/ MAXimum / RAW=内存波形（**要求示波器处于
    STOP 态**，否则报 device_error 并提示先 `mho_acquisition("stop")`）；
    fmt: BYTE(8bit)/WORD(16bit)/ASCii；points 受模式上限约束（超限报 param_validation）。
    save_csv=True 存 CSV 到 TEST_DATA/mho/（不返回完整数组，防上下文爆炸）。
    **分析频率请用 FFT**（朴素过零对调幅信号会误判）。

    换算公式与实测上限见 skill instrument-mcp →「参数语义速查 · mho_get_waveform」。
    """
    def fn(s: MHO):
        wf = s.get_waveform(ch, mode=mode, fmt=fmt, points=points)
        vs = wf["v"]
        out = {
            "ch": ch, "mode": wf["mode"], "format": wf["format"],
            "points": wf["points"], "xinc_s": wf["xinc"], "xorigin_s": wf["xorigin"],
            "v_min": min(vs), "v_max": max(vs), "vpp": max(vs) - min(vs),
            "t_start_s": wf["t"][0], "t_end_s": wf["t"][-1],
        }
        if save_csv:
            p = Path(ROOT) / "TEST_DATA" / "mho" / (
                f"mcp_mho_wave_ch{ch}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["time_s", "voltage_v"])
                w.writerows(zip(wf["t"], wf["v"]))
            out["csv"] = str(p)
        return out
    return _call("MHO", lambda: _mho(resource), fn)


@device_tool()
def mho_acquisition(action: str, resource: str | None = None) -> str:
    """MHO 采集控制：action ∈ run（连续采集）/ stop（冻结，**RAW 读内存波形前必须**）/
    single（单次触发）/ force（强制触发一次）。

    ⚠ stop 会冻结当前采集——共享实验台上可能打断他人的观察；run/single/force 同理
    会改变采集状态。仅改采集状态，不动通道档位/时基/触发配置。
    返回 {"action", "trigger_status"}（设备回读）。"""
    act = action.strip().lower()
    if act not in ("run", "stop", "single", "force"):
        return _err("param_validation",
                    f"未知 action {action!r}（run|stop|single|force）", "MHO")

    def fn(s: MHO):
        {"run": s.run, "stop": s.stop,
         "single": s.single, "force": s.force_trigger}[act]()
        time.sleep(0.3)
        return {"action": act, "trigger_status": s.trigger_status()}
    return _call("MHO", lambda: _mho(resource), fn)


@device_tool()
def mho_autoset(confirm: bool = False, resource: str | None = None) -> str:
    """MHO 一键自动设置 `:AUToset`（手册 3.2.1）。⚠ **全局破坏性**：会重新调整
    **所有**通道的垂直档位、水平时基与触发配置——多信号实验台上会毁掉别人已调好的
    通道。必须 confirm=True；且确认"信号简单周期性 + 无其他在用通道"再用。
    无波形时的推荐顺序：先 mho_status / mho_screenshot 看真相，再决定是否 autoset。"""
    if not confirm:
        return _err("confirm_required",
                    ":AUToset 会重置所有通道档位/时基/触发（全局破坏性），需 confirm=True",
                    "MHO")

    def fn(s: MHO):
        s.autoset()
        time.sleep(2.5)
        return {"autoset": True, "trigger_status": s.trigger_status(),
                "timebase_s_div": s.timebase_scale()}
    return _call("MHO", lambda: _mho(resource), fn)


# ============ DG832 信号源（RIGOL DG800 系列；2026-09-15 由独立 instrument 服务器并入） ============
#
# 库：dg832_control（唯一维护点）。该库自带两套**硬件保护**，并入时原样保留：
#   ① protect 联锁：设幅度/偏移或开输出前必须已开启有效电压保护，否则 protect_required；
#   ② DC 切换快照：set_dc_only 返回切换前配置，切回时要求显式传参（不做隐式恢复）。
# 仓库侧新增的只有 dg_output 的 confirm 门（AGENTS.md 红线：开关输出需授权）。

@device_tool()
def dg_status(model: str | None = None, resource: str | None = None) -> str:
    """DG832 快照：设备信息（型号/序列号/固件）+ CH1/CH2 当前波形配置（波形/频率/
    幅度/偏移/相位）、输出开关、负载。**动这台之前先查它**。

    model: 同族型号可选（DG811/812/821/822/831/832 同命令集，省略默认 DG832；
    未登记型号返回 model_unsupported）。"""
    return _call("DG832", lambda: _dg(resource, model), lambda g: g.status())


@device_tool()
def dg_protect(ch: int, high: float | None = None, low: float | None = None,
               state: bool | None = None, model: str | None = None,
               resource: str | None = None) -> str:
    """DG832 输出电压保护（防超压）：设上限/下限（V）与开关。

    ⚠ **强制流程**：设置幅度/偏移（`dg_set_wave`/`dg_set_param` 的 amp/offset）或打开输出
    （`dg_output` on）之前，必须先用本工具开启有效保护（`state=True` 且 `high>low`），
    否则返回 `protect_required` 被拒；保护开启后越界设置返回 `protect_range`，
    不会静默超压。省略的参数保持当前值；全省略 = 仅查询。
    """
    return _call("DG832", lambda: _dg(resource, model),
                 lambda g: g.set_voltage_limit(ch, high=high, low=low, state=state))


@device_tool()
def dg_get_protect(ch: int, model: str | None = None,
                   resource: str | None = None) -> str:
    """DG832 查询指定通道的电压保护配置（开关/上限/下限）。"""
    return _call("DG832", lambda: _dg(resource, model),
                 lambda g: g.get_voltage_limit(ch))


@device_tool()
def dg_set_wave(ch: int, shape: str, freq: float | None = None, amp: float | None = None,
                offset: float | None = None, phase: float | None = None,
                sample_rate: float | None = None, model: str | None = None,
                resource: str | None = None) -> str:
    """DG832 快速设置波形（多参数一条 `:APPL` 命令）。ch=1-2。

    shape: sine/square/ramp/pulse/dc/noise/prbs/user(任意波)/harmonic/dualtone/rs232/sequence；
    freq(Hz) / amp(Vpp) / offset(Vdc) / phase(°0-360)；sequence 波首个参数是采样率 sample_rate。
    省略的参数**保持当前值**（先读当前配置填充，不会重置为默认）。
    ⚠ 设 amp/offset 前需已开保护（见 dg_protect），否则 protect_required。
    本工具**不改变输出开关状态**。"""
    return _call("DG832", lambda: _dg(resource, model),
                 lambda g: g.set_wave(ch, shape, freq, amp, offset, phase, sample_rate))


@device_tool()
def dg_set_param(ch: int, param: str, value: str, model: str | None = None,
                 resource: str | None = None) -> str:
    """DG832 单参数设置并读回。param: freq(Hz)/amp(Vpp)/offset(Vdc)/phase(°)/load(Ω，inf=高阻)。
    amp/offset 会做电压保护范围校验（需先开保护）。设备静默钳制时返回体 note 会提示。"""
    def fn(g):
        p = param.strip().lower()
        if p == "freq":
            return g.set_freq(ch, float(value))
        if p == "amp":
            return g.set_amp(ch, float(value))
        if p == "offset":
            return g.set_offset(ch, float(value))
        if p == "phase":
            return g.set_phase(ch, float(value))
        if p == "load":
            return g.set_load(ch, value if value.strip().lower() == "inf" else float(value))
        raise ValueError("param 必须是 freq/amp/offset/phase/load")
    return _call("DG832", lambda: _dg(resource, model), fn)


@device_tool()
def dg_set_dc(ch: int, level: float, model: str | None = None,
              resource: str | None = None) -> str:
    """DG832 DC 专用切换：设直流电平（V）并返回切换前配置快照（restore: shape/freq/amp/
    offset/phase）。已在 DC 时只改电平、restore 为 None。
    切回非 DC 时请用 restore 值**显式**传参（库不做隐式恢复，避免非预期写入）。"""
    return _call("DG832", lambda: _dg(resource, model),
                 lambda g: g.set_dc_only(ch, level))


@device_tool()
def dg_sweep(ch: int, start: float | None = None, stop: float | None = None,
             time: float | None = None, spacing: str | None = None,
             step: int | None = None, htime_start: float | None = None,
             htime_stop: float | None = None, rtime: float | None = None,
             trig_source: str | None = None, state: bool | None = None,
             model: str | None = None, resource: str | None = None) -> str:
    """DG832 频率扫频配置/开关/查询。全省略且 state=None = 仅查询当前配置。

    start/stop(Hz，双向皆可，须在当前波形频率上限内)；time(s，1ms~500s)；
    spacing: lin/log/step；step(2~1024，仅 step 间隔)；
    htime_start/htime_stop/rtime(s)；trig_source: int/ext/man；
    state: True=开启（设备会自动关闭调制/脉冲串）/False=关闭/None=不改。
    仅 sine/square/ramp/user 支持扫频。"""
    return _call("DG832", lambda: _dg(resource, model),
                 lambda g: g.set_sweep(ch, start, stop, time, spacing, step,
                                       htime_start, htime_stop, rtime, trig_source, state))


@device_tool()
def dg_sweep_trigger(ch: int, model: str | None = None,
                     resource: str | None = None) -> str:
    """DG832 手动触发一次扫频（需触发源为 manual 且该通道输出已打开）。"""
    return _call("DG832", lambda: _dg(resource, model), lambda g: g.sweep_trigger(ch))


@device_tool()
def dg_output(ch: int, on: bool, confirm: bool = False, model: str | None = None,
              resource: str | None = None) -> str:
    """DG832 开关通道 ch(1-2) 输出。⚠ **开/关都需 confirm=True**——关断同样可能打断
    正在进行的测试或他人实验（授权语义与 `sdg_output`/`psu_output` 一致）。

    打开前需已开启有效电压保护（见 `dg_protect`），否则被拒（库内联锁）。
    建议先 `dg_status` 查当前输出状态与负载。"""
    if not confirm:
        return _err("confirm_required",
                    f"输出开关（{'ON' if on else 'OFF'}）需 confirm=True——"
                    f"关闭同样可能打断正在进行的测试/实验", "DG832")
    return _call("DG832", lambda: _dg(resource, model), lambda g: g.output(ch, on))


@device_tool()
def dg_counter(model: str | None = None, resource: str | None = None) -> str:
    """DG832 内置频率计测量（面板 [Counter] 输入口信号），返回频率等读数。"""
    def fn(g):
        g.counter_on(True)
        return g.counter_measure(timeout=2.0)
    return _call("DG832", lambda: _dg(resource, model), fn)


@device_tool()
def dg_query(scpi: str, model: str | None = None, resource: str | None = None) -> str:
    """DG832 只读 SCPI 查询（如 `:SOUR1:APPL?`、`:OUTP1?`、`:MEASure:ITEM? VPP,CHANnel1`）。
    整条必须是纯查询——判据同 instr_query：每条 `;` 分段的**命令头带 '?'** 即可，
    **问号后允许带参数**；写命令（头里无 '?'）与复位/锁定类一律拒绝。"""
    cmd = scpi.strip()
    if not _is_query_only(cmd) or _is_forbidden(cmd):
        return _err("param_validation" if "?" not in cmd else "forbidden",
                    f"dg_query 只接受纯查询消息（问号后可以带参数；不得含写命令/复位/锁定类）: {scpi!r}",
                    "DG832")
    return _call("DG832", lambda: _dg(resource, model), lambda g: g.query(cmd))


@device_tool()
def dg_check_error(model: str | None = None, resource: str | None = None) -> str:
    """DG832 查询并清空设备错误队列（空列表 = 正常）。命令疑似被拒后用它诊断。"""
    def fn(g):
        errs = g.check_error()
        return errs or []
    return _call("DG832", lambda: _dg(resource, model), fn)


# ============ DH1766 电源 ============

@device_tool()
def psu_status(resource: str | None = None) -> str:
    """DH1766 只读状态总览（**操作电源前先调这个**）：三路(CH1-3)电压/电流/功率/
    设定值/OVP/OCP/输出状态/**输出模式**/耦合，并附**安全检查**：
    safe + warnings（TRAC 负压跟随 / OVP·OCP ≤ 设定值 / 已有通道带电 /
    QUES 寄存器告警 / 通道耦合）。
    （原 psu_measure 的电压电流读数已并入本工具，原 psu_pre_check 的安全判断
    亦并入 warnings。）"""
    def fn(p):
        snap = p.snapshot()
        check = p.pre_power_check()
        return {"state": snap, "safe": check["safe"], "warnings": check["warnings"]}
    return _call("DH1766", lambda: _psu_connect(resource), fn, close_fn=_psu_close)


@device_tool()
def psu_mode(resource: str | None = None) -> str:
    """DH1766 输出模式查询（只读，轻量）：NORM（正常三路独立）/TRAC（跟踪：
    CH2 跟随 CH1 输出同等值负电压）/SERI（串联）/PARA（并联）。
    操作电源前先查模式——CH2 负压是跟踪模式跟随，不是固定负轨（手册§3.8）。
    需要完整状态用 psu_status。"""
    return _call("DH1766", lambda: _psu_connect(resource),
                 lambda p: {"output_mode": p.output_mode()},
                 close_fn=_psu_close)


@device_tool()
def psu_output(ch: int, on: bool, expect_mode: str, confirm: bool = False,
               resource: str | None = None) -> str:
    """DH1766 单通道输出开关。⚠ 开/关都需 confirm=True（关闭可能中断供电，
    影响被测电路/他人实验，同样是状态变更）。

    **expect_mode 必填**：调用方声明的当前工作模式（NORM/TRAC/SERI/PARA），
    仅校验不设置——与实际不符立即拒绝并回传当前模式（防拓扑误判：TRAC 下
    CH2 跟随 CH1 输出负压、SERI/PARA 通道合并）。建议先调 psu_status。
    """
    if not confirm:
        return _err("confirm_required",
                    f"输出开关（{'ON' if on else 'OFF'}）需 confirm=True——"
                    f"关闭可能中断供电影响被测电路", "DH1766")

    def fn(p):
        p.set_output(ch, on, expect_mode)
        time.sleep(0.3)
        return {"output_on": p.get_output_state(), "output_mode": p.output_mode()}

    return _call("DH1766", lambda: _psu_connect(resource), fn,
                 close_fn=_psu_close)


@device_tool()
def psu_set_mode(mode: str, resource: str | None = None) -> str:
    """DH1766 设置输出模式：NORM/TRAC/SERI/PARA（写后回读比对）。
    ⚠ 继电器联动拓扑变化：输出必须全关，否则直接拒绝（库内无条件强制）。
    切换范例：跟踪 ±12V 供电用 TRAC；单路独立用 NORM。"""
    def fn(p):
        p.set_output_mode(mode)
        return {"output_mode": p.output_mode()}
    return _call("DH1766", lambda: _psu_connect(resource), fn,
                 close_fn=_psu_close)


@device_tool()
def psu_power_cycle(ch: int, expect_mode: str, cycles: int = 1,
                    off_delay_s: float = 1.0, on_delay_s: float = 1.0,
                    confirm: bool = False, resource: str | None = None) -> str:
    """DH1766 上下电循环：关断→延迟→开启→延迟，重复 cycles 次。

    ⚠ **需 confirm=True**——授权同 psu_output：① 用户本轮明确要求做上下电/循环，
    或 ② 用户明确声明独占使用；否则先向用户确认（可能打断他人实验/板子供电）。

    **expect_mode 必填**（仅校验，不符拒绝）。
    off_delay_s 默认 1s（需保证下电放电时调大，如实测电容残留需 ≥6s）；on_delay_s
    默认 1s；cycles 默认 1。延迟由主机 sleep 控制，**不精准**，仅用于保证放电/上电时序。
    返回 {"cycles", "records":[每次循环延迟与电压], "after"}。
    """
    if not confirm:
        return _err("confirm_required",
                    "上下电循环需 confirm=True——授权来源：用户明确要求做上下电循环，"
                    "或用户声明独占使用；否则先确认（可能打断他人实验/板子供电）",
                    "DH1766")

    def fn(p):
        return p.power_cycle(ch, expect_mode, off_delay_s=off_delay_s,
                             on_delay_s=on_delay_s, cycles=cycles)

    return _call("DH1766", lambda: _psu_connect(resource), fn,
                 close_fn=_psu_close)


def _prewarm_visa_rm() -> None:
    """后台预热 VISA 运行时（visa32.dll 加载），历史上实测可 20s+。

    ⚠ 本线程内禁止任何 Python import（实测 2026-09-03：mcp.run() 事件循环与
    后台线程 import pyvisa 互卡死锁——线程永远停在 import 上，首个设备工具
    调用连锁冻结）。因此 import 一律在主线程模块加载期同步完成（实测 <1s），
    线程只做 ResourceManager 初始化这个纯 DLL 调用。
    """
    try:
        rm = pyvisa.ResourceManager()
        try:
            rm.close()
        except Exception:
            pass
    except Exception:
        pass
    _PREWARM_DONE.set()


# 主线程同步预热：pyvisa + 五设备库（含 numpy 等重依赖）。
# 必须先于 prewarm 线程与 mcp.run()——见 _prewarm_visa_rm 死锁注记。
import pyvisa  # noqa: E402
from common.visa_client import VisaClient  # noqa: E402
import sds_control  # noqa: E402,F401
import sdg_control  # noqa: E402,F401
import keysight_3446x  # noqa: E402,F401
import keysight_3458a  # noqa: E402,F401
import dho_control  # noqa: E402,F401
import mho_control  # noqa: E402,F401
import dg832_control  # noqa: E402,F401

threading.Thread(target=_prewarm_visa_rm, daemon=True).start()


def _run_server() -> None:
    """按 `MCP_TRANSPORT` 环境变量选择传输方式（默认 stdio，行为与改动前一致）。

    - `stdio`（默认，含未设置）：每个客户端一个子进程、由 OpenCode 自己拉起 —— 回滚路径；
    - `streamable-http`：**常驻单例** HTTP 服务（多个客户端 / 项目共用一份进程），
      监听 `MCP_HTTP_HOST`:`MCP_HTTP_PORT`（默认 127.0.0.1:8765，路径 /mcp）；
    - 其它值：显式报错退出。**不做静默回退** —— 静默回退会把"配置写错"伪装成
      "服务正常"，排查时极难定位；配置错误必须响亮。

    默认只绑定回环地址；需要对外暴露要显式改 `MCP_HTTP_HOST`（并自担安全责任）。
    """
    transport = (os.environ.get("MCP_TRANSPORT") or "stdio").strip().lower()
    if transport == "stdio":
        mcp.run()
        return
    if transport != "streamable-http":
        raise SystemExit(
            f"unknown MCP_TRANSPORT={transport!r} "
            "(expected 'stdio' or 'streamable-http')")
    host = os.environ.get("MCP_HTTP_HOST", "127.0.0.1")
    try:
        port = int(os.environ.get("MCP_HTTP_PORT", "8765"))
    except ValueError as exc:
        raise SystemExit(f"invalid MCP_HTTP_PORT: {exc}") from exc
    mcp.settings.host = host
    mcp.settings.port = port
    print(f"[instrumentControl] transport=streamable-http "
          f"listening on http://{host}:{port}{mcp.settings.streamable_http_path}",
          file=sys.stderr, flush=True)
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    # 启动自检（P0-1③，docs/tool_optimization_20260915.md）：把"查询式判据"的实测结果
    # 打一行到 **stderr**。历史事故：磁盘代码已改、跑着的进程仍是旧规则，调用方被误导为
    # "工具不支持参数化查询"，白绕一圈。stdout 是 JSON-RPC 协议通道，绝不能打印。
    # 2026-09-22：本段输出一律 **ASCII 英文**（非 ASCII 字符一个都不留）。
    # 原因：中文 Windows 下 Python 的 stderr 默认 GBK(cp936)，这些字节被 DSH 侧终端
    # 按 UTF-8 解码 → 替换字符 U+FFFD → xterm.js "Parsing error: code 65533" 噪音，
    # traceback 的路径行还会被吃掉半截。异常文本用 ascii() 转义，整行保证纯 ASCII。
    # 文案要与上游断言兼容：verify_mcp_tools_meta.py / verify_mcp_ks3458a_tools.py
    # 用「description + 空白 + 数字」判「缺口为 0」，故保留 `description <n>` 空格写法。
    try:
        print("[instrumentControl] startup self-check | parametric query "
              f"':MEASure:ITEM? VPP,CHANnel1' -> query_only="
              f"{_is_query_only(':MEASure:ITEM? VPP,CHANnel1')} | "
              "multi-segment readback "
              "':CHANnel4:DISPlay?;:CHANnel4:SCALe?' -> query_only="
              f"{_is_query_only(':CHANnel4:DISPlay?;:CHANnel4:SCALe?')} | "
              "write smuggled in ':CHANnel4:DISPlay?;:OUTP4 ON' -> "
              f"query_only={_is_query_only(':CHANnel4:DISPlay?;:OUTP4 ON')} "
              "(False = rejected) | reset '*RST' -> "
              f"forbidden={_is_forbidden('*RST')}",
              file=sys.stderr, flush=True)
    except Exception as e:  # 自检失败不影响服务启动
        print("[instrumentControl] startup self-check failed: "
              f"{type(e).__name__}: {ascii(e)}",
              file=sys.stderr, flush=True)
    # 工具元信息自检（2026-09-16 加）：**每个工具的 description 来自其函数 docstring**，
    # 漏写 docstring 的工具会在客户端里显示成"无描述"（历史上退役的 DG832 独立服务器
    # 13 个工具里就有 12 个没描述，被误当成"工具集缺文档"）。这里启动时报出总数与缺口。
    # 同口径的协议级断言在 TEST_SCRIPTS/common/verify_mcp_tools_meta.py（离线可跑）。
    try:
        _tools = mcp._tool_manager.list_tools()
        _no_desc = [t.name for t in _tools if not (t.description or "").strip()]
        print(f"[instrumentControl] startup self-check | tools={len(_tools)}, "
              f"missing description {len(_no_desc)}"
              + (f" {_no_desc}" if _no_desc else " (all present)"),
              file=sys.stderr, flush=True)
    except Exception as e:
        print("[instrumentControl] tool metadata self-check skipped: "
              f"{type(e).__name__}: {ascii(e)}",
              file=sys.stderr, flush=True)
    _run_server()
