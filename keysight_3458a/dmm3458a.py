"""HP/Keysight 3458A 八位半万用表驱动（**专用实现**，不套 SCPI 假设）。

与 `keysight_3446x.DMM`（34465A，标准 SCPI）的关键差异：

    - 身份用 `ID?`（无 `*IDN?`）、错误用 `ERRSTR?`（无 `SYST:ERR?`）、复位用 `RESET`；
    - 单次读数是 `TARM SGL,1` 触发后直接回值（命令不带问号）；
    - 档位/NPLC 用 `DCV <range>` / `NPLC <n>`，且**没有档位回读命令**——
      驱动只能记录"本会话设过什么"，不能问设备"你现在是几档"（如实报告，不猜）；
    - 高速采样走 `PRESET DIG` + SINT 二进制突发（2 字节大端有符号 × ISCALE）。

三条来自参考实现（EmoeCalibrator）的硬性经验，已固化在下面各方法里：

    1. **换档/换配置后必须丢弃第一次读数**（建立时间 + 自校准，[SICL] L339-341）；
    2. **会话恢复要先 IFC/clear 再有限 drain**，否则 free-run 的表能把你读到 91 s（[SICL] L273-276）；
    3. `RESET` 之后要补 `END ALWAYS` + `INBUF ON`，否则 `TARM SGL` 会占住 GPIB 总线（[VISA] L571-576）。

连接**默认不改设备状态**（`reset_on_open=False`）：MCP 是无状态工具，不该每次
RESET 仪表。要重置另走显式 `reset()`（MCP `ks3458a_reset` 需 confirm=True）。
"""
from __future__ import annotations

import os
import re
import time
from typing import Optional

from . import commands as C
from .transport import (
    DRAIN_MAX_ROUNDS,
    DRAIN_TIMEOUT_MS,
    Transport,
    TransportError,
    make_transport,
)


def _first_float(text: str) -> float:
    """从响应里取第一个数值（`'1.4E-07'` / `'-5.23E-05 VDC'` → float）。

    3458A 的响应可能带单位后缀或前导状态字段，取第一个可解析数字比整体 `float()` 稳
    （与 `keysight_3446x.dmm._num` 同口径）。没有数字就报错，**不返回 0 兜底**。
    """
    match = re.search(r"[-+]?\d*\.?\d+(?:[EeDd][-+]?\d+)?", text or "")
    if match is None:
        raise TransportError(f"响应里没有可解析的数值: {text!r}")
    return float(match.group().replace("D", "E").replace("d", "e"))


def _safe_float(text) -> Optional[float]:
    """尽力把响应转 float；失败返回 None（状态回读用：单项失败不该毁整次查询）。"""
    if text is None:
        return None
    try:
        return _first_float(str(text))
    except Exception:                                # noqa: BLE001
        return None


def _check_range(dcv_range):
    """档位：数值必须是 3458A 直流电压档位全集之一；字符串 `AUTO` = 自动挡。

    [MANUAL] p.183-184：`FUNC`/`DCV` 的 `max._input` 给数值=固定档、给 `AUTO`=自动挡；
    另有 `ARANGE ON|OFF|ONCE`（p.160-161）可单独开关自动挡。
    """
    if isinstance(dcv_range, str):
        text = dcv_range.strip().upper()
        if text in ("AUTO", "AUTORANGE"):
            return "AUTO"
    value = float(dcv_range)
    for allowed in C.DCV_RANGES:
        if abs(value - allowed) < 1e-9:
            return allowed
    raise ValueError(
        f"不支持的档位 {dcv_range!r}；3458A 直流电压档位只有 {list(C.DCV_RANGES)} V，"
        f"或传 'AUTO' 用自动挡")


def _check_nplc(nplc) -> float:
    """NPLC 必须为正数。**上限不做猜测**：交给设备判（报了错用 `ERRSTR?` 看）。"""
    value = float(nplc)
    if not value > 0:
        raise ValueError(f"NPLC 必须 > 0，收到 {nplc!r}")
    return value


def _stats(values: list[float]) -> dict:
    """均值 / 样本标准差 / 极值（n=1 时标准差记 0.0，与参考实现一致）。"""
    count = len(values)
    mean = sum(values) / count
    if count > 1:
        stddev = (sum((v - mean) ** 2 for v in values) / (count - 1)) ** 0.5
    else:
        stddev = 0.0
    return {"n": count, "mean": mean, "stddev": stddev,
            "min": min(values), "max": max(values)}


def is_error_clear(errtxt: str) -> bool:
    """`ERRSTR?` 响应是否表示"无错误"（形如 `0,"NO ERROR"`）。

    判据只看**错误码部分是否为 0**（不认措辞，措辞因固件而异）。解析不出来时返回
    False——拿不准就当有错报给调用方看原文，不替它下结论。

    ⚠ 3458A 的错误队列语义（是否弹出一条、队列深度、如何清空）**待手册核对**，
    见 `docs/COMMANDS_3458A.md`。
    """
    match = re.match(r"\s*([+-]?\d+)\s*,", errtxt or "")
    return bool(match) and int(match.group(1)) == 0


class DMM3458A:
    """3458A 会话（connect → 配置/读数 → close）。

    用法::

        with DMM3458A("GPIB0::9::INSTR") as d:      # 或 "sicl:gpib0,9"
            print(d.idn())                          # HP3458A
            d.configure_dcv(10.0, 10.0)             # 10V 档 / 10 PLC（换档后丢首读数）
            print(d.read_dcv())

    离线测试/自定义传输：给 `transport=` 一个实现 `transport.Transport` 的对象，
    此时 `resource` 可为 None（**不会**打开任何真实设备）。
    """

    def __init__(self, resource: Optional[str] = None, timeout_s: float = 30.0,
                 dcv_range: float = 10.0, nplc: float = 10.0,
                 reset_on_open: bool = False,
                 transport: Optional[Transport] = None):
        self.resource = resource
        self.timeout_s = float(timeout_s)
        self.dcv_range = _check_range(dcv_range)     # 期望档位（configure 时下发）
        self.nplc = _check_nplc(nplc)
        # MCP 路径必须保持 False：无状态工具不该每次调用都 RESET 仪表
        self.reset_on_open = bool(reset_on_open)
        self._transport = transport
        self._connected = False
        self.recover_mode: bool | str = "auto"       # connect() 决定；"auto"=失败才恢复
        self.holders: list[dict] = []                # 连接时发现的**他人**占用（辅助锁）
        self._range: Optional[float] = None          # 本会话已下发过的档位（None=没设过，'AUTO'=自动挡）
        self._autorange: Optional[bool] = None       # 本会话是否发过 ARANGE ON/OFF（None=没动过）
        self._nplc: Optional[float] = None           # 本会话已下发过的 NPLC（None=没设过）

    # ---------- 连接 ----------
    def _claim(self) -> list[dict]:
        """登记"本进程正在用该资源"（`common/session_lock` 辅助锁），返回**他人**占用列表。

        锁是**辅助设施**：任何异常都降级为空列表，绝不让它挡住正常使用。
        意义：脚本用库直连时也能被 MCP/AI 侧看到；反之亦然——避免"同一台表两个会话
        静默串台"（实测过：互相读到对方的响应）。

        ⚠ 进程外 worker（`keysight_3458a.worker`）里**不要**再登记：父进程（MCP）已经登记过，
        子进程再登记会让父进程把自己人看成"另一个进程在用"（实测产生假告警）。
        父进程通过环境变量 `INSTRUMENT_NO_SESSION_LOCK=1` 告知子进程跳过。
        """
        if str(os.environ.get("INSTRUMENT_NO_SESSION_LOCK", "")).strip() in ("1", "true", "yes"):
            return []
        try:
            from common import session_lock

            info = session_lock.touch(self.resource, kind="ks3458a")
            return list(info.get("holders") or [])
        except Exception:                            # noqa: BLE001
            return []

    def _release_claim(self) -> None:
        try:
            from common import session_lock

            session_lock.release(self.resource)
        except Exception:                            # noqa: BLE001
            pass

    def connect(self, recover: bool | str = "auto", force: bool = False) -> str:
        """打开会话，返回资源串（与 keysight_3446x / rigol_scope 的 connect 一致）。

        `recover` 三态（2026-09-23 性能实测后改成"按需恢复"）：

        * `"auto"`（默认，**快**）：只做 `prepare_for_read()`（3 条写，≈0.01 s）。
          真正的会话恢复推迟到**读数失败时**由 `_with_recover_retry()` 触发一次并重试。
          原因：恢复里的两轮有界 drain 各要 ~2 s（VISA 最小超时 ≈2 s，250 ms 设不下去），
          而"表被留在 free-run"是**少数情况**——不该让每次调用都付 5 s。
        * `True`：**强制**先做完整恢复（`recover()`，慢，≈5 s）。排障/共享实验台被人
          留下 free-run 时用；`TEST_SCRIPTS` 的恢复用例也走这条。
        * `False`：既不恢复也不重试（只读探测/极限低延迟场景，风险自负）。

        ⚠ 恢复与 `prepare_for_read()` 都会碰总线状态；SICL 通路的 IFC 还影响**整条
        GPIB 总线**（本机 GPIB0 上只有这台 3458A）。

        `force=False`（默认）时，若辅助锁显示**别的进程正在用同一地址**，直接报
        `TransportError` 并列出占用者——同一台表两个会话会**静默串台**（实测过），
        所以宁可拒绝也不并发。确有理由要抢（对方已死/僵尸锁）才传 `force=True`。

        `reset_on_open=True` 时额外 `reset()` + 按构造参数配置档位/NPLC（**破坏性**）。
        """
        if self._transport is None:
            if not self.resource:
                raise ValueError(
                    "resource 为空：需要 GPIB/VISA/SICL 资源串，"
                    "如 'GPIB0::9::INSTR' / 'visa://<host>/GPIB0::9::INSTR' / 'sicl:gpib0,9'")
            self._transport = make_transport(self.resource, timeout_s=self.timeout_s)
        # 先登记自己的占用，再看有没有别人（顺序反了会把自己也算进去）
        self.holders = self._claim()
        if self.holders and not force:
            who = "; ".join(
                f"pid={h.get('pid')} kind={h.get('kind') or '?'} host={h.get('host') or '?'}"
                f" started={h.get('started')}" for h in self.holders[:3])
            raise TransportError(
                f"3458A 正被其它会话占用（{len(self.holders)} 个）：{who}。"
                f"同一地址并发会静默串台，已拒绝连接——请等对方释放；"
                f"确认对方已死/僵尸锁时可用 connect(force=True) 强制")
        try:
            self._transport.open()
        except Exception:
            self._release_claim()                    # 开不起来就别占着锁
            raise
        self._connected = True
        self.recover_mode = recover
        if recover is True:
            self.recover()
        if self.reset_on_open:
            self.reset()
            self.configure_dcv(self.dcv_range, self.nplc)
        else:
            # 没 RESET 时，recover() 会把触发挂起（TRIG HOLD）——而 `TARM SGL,1`
            # 需要一个可用的触发源，否则读数永远不产生。
            # 2026-09-23 本机实测：TRIG? = 4(HOLD) 时 `TARM SGL,1` 20 s 超时；
            # 补发 `TRIG AUTO` 后立刻出数（0.43 s/次 @NPLC=10）。这里按需准备。
            self.prepare_for_read()
        return self.resource

    def _with_recover_retry(self, fn):
        """先按快速路径执行 `fn()`；失败且允许恢复时，做一次完整恢复 + 重试一次。

        "失败"= 超时/传输错/响应里没有可解析数值（free-run 会让读数错位）——
        正是"表被留在 free-run"的现场特征。恢复后重试用同一把锁、同一会话。
        """
        try:
            return fn()
        except Exception as first:
            if self.recover_mode is False:
                raise
            try:
                self.recover()
                self.prepare_for_read()
            except Exception:                        # noqa: BLE001 —— 恢复失败就报原始错
                raise first
            try:
                return fn()
            except Exception as second:              # noqa: BLE001
                raise TransportError(
                    f"读数失败且会话恢复后仍失败：首次 {type(first).__name__}: {first}；"
                    f"重试 {type(second).__name__}: {second}") from second

    def prepare_for_read(self) -> None:
        """`TARM SGL` 单次读数配方的**前置准备**（不改档位/NPLC/功能）。

        发 `END ALWAYS` + `INBUF ON`（手册 TARM 章节要求，缺了会占住 GPIB 总线）+
        `TRIG AUTO`（恢复自动触发源）。参考实现走的是 `RESET`——`RESET` 会把触发源
        复位成 AUTO；我们为了不动用户设定不发 `RESET`，所以在这里显式补上。
        """
        transport = self._t()
        transport.write(C.END_ALWAYS)
        transport.write(C.INBUF_ON)
        transport.write(C.TRIG_AUTO)
        time.sleep(0.2)

    def close(self, *, release: bool = False) -> None:
        """关会话。`release=True` 时先 `release_to_front_panel()`（面板连续刷新）。

        默认仍发 `TARM HOLD`（本会话还要继续远程读的保守收尾）；但**脚本最后一次
        用完仪器**应显式 `release=True`（或先调 `release_to_front_panel()` 再 close），
        否则面板停在冻结读数 + MRNG（2026-10-08 现场教训）。
        """
        transport = self._transport
        if transport is None:
            return
        try:
            if release:
                self.release_to_front_panel()
            else:
                transport.write(C.TARM_HOLD)
        except Exception:                            # noqa: BLE001 —— 收尾尽力而为
            pass
        try:
            transport.close()
        finally:
            self._connected = False
            self._release_claim()                    # 交还辅助锁（让 MCP/其它脚本能看到设备空了）

    @property
    def is_open(self) -> bool:
        return self._connected

    def __enter__(self) -> "DMM3458A":
        if not self._connected:
            self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _t(self) -> Transport:
        if self._transport is None or not self._connected:
            raise TransportError("3458A 未连接，请先 connect()")
        return self._transport

    # ---------- 身份 / 状态 ----------
    def idn(self) -> str:
        """`ID?`（3458A **没有** `*IDN?`）。实测返回形如 `HP3458A`。"""
        return self._t().query(C.ID)

    def error_string(self) -> str:
        """`ERRSTR?`（3458A **没有** `SYST:ERR?`）。返回原始响应，不做解读。

        已知形态 `<code>,"<message>"`（无错误为 `0,"NO ERROR"`）；每次查询弹出一条
        （FIFO）。队列深度与清空方式待手册核对——白名单里没有清队列命令。
        需要判断时用 `is_error_clear()`。
        """
        return self._t().query(C.ERRSTR)

    def temperature(self) -> float:
        """`TEMP?` 内部温度读数（数值，单位 **°C**）。

        2026-09-23 本机 GPIB0::9 实测返回 `37.0` / `36.9`（开机数小时后）——数值合理，
        故单位按 °C 采信；精度与是否需要温度选件仍未核对手册。
        """
        return _first_float(self._t().query(C.TEMP))

    def state(self) -> dict:
        """**设备回读**的当前状态（不是"本会话设过什么"的记录）。

        2026-09-23 本机实测：下列查询在 3458A 上都有响应（响应样例见 `commands.py`）；
        同日又按手册逐条核对（`docs/3458a_manual_verification_20260923.md`），故这里把
        裸码值**解码**成可读语义（`4(HOLD)` / `1(ASCII)` / `4(SREAL)`）——避免再出现
        "MFORMAT?=4 当成 SINT"这类误读（手册 p.199：4 = SREAL，SINT = 2）。
        任一查询失败只记 `None`，不让整次状态查询失败。
        """
        t = self._t()

        def q(cmd: str):
            try:
                return t.query(cmd)
            except Exception:                        # noqa: BLE001
                return None

        def dec(table: dict, raw):
            """裸码值 → `'4(HOLD)'`（码表来自手册，见 commands.py 的 *_CODES）。"""
            if raw is None:
                return None
            try:
                n = int(float(str(raw).strip()))
                return f"{n}({table[n]})"
            except Exception:                        # noqa: BLE001 —— 非预期值原样返回
                return raw

        _arange_raw = q(C.ARANGE_Q)                      # 自动挡状态只信这条（见下）

        out: dict = {
            "id": q(C.ID),
            "error": q(C.ERRSTR),
            "tarm": dec(C.TARM_CODES, q(C.TARM_Q)),          # 1(AUTO)/4(HOLD)…
            "trig": dec(C.TRIG_CODES, q(C.TRIG_Q)),          # 1(AUTO)/4(HOLD)…
            "nrdgs": q(C.NRDGS_Q),                           # '1, 1'（第二字段=采样事件码，AUTO=1）
            "nplc": _safe_float(q(C.NPLC_Q)),
            "aperture_s": _safe_float(q(C.APER_Q)),
            "function": q(C.FUNC_Q),                         # '1, .1' = DCV + max_input
            "range_v": _safe_float(q(C.RANGE_Q)),
            # 自动挡状态**只信 ARANGE?**：2026-09-23 实测 `DCV AUTO`/`ARANGE ON` 之后
            # `FUNC?` 仍返回固定档数值（'1, .1'），只有 ARANGE? 变化（1=ON / 0=OFF）。
            "arange": dec(C.ARANGE_CODES, _arange_raw),
            "autorange": (str(_arange_raw).strip() == "1") if _arange_raw is not None else None,
            "azero": dec(C.AZERO_CODES, q(C.AZERO_Q)),
            "mem": q(C.MEM_Q),
            "inbuf": dec(C.INBUF_CODES, q(C.INBUF_Q)),
            "end": dec(C.END_CODES, q(C.END_Q)),
            "oformat": dec(C.FORMAT_CODES, q(C.OFORMAT_Q)),
            "mformat": dec(C.FORMAT_CODES, q(C.MFORMAT_Q)),
            "iscale": _safe_float(q(C.ISCALE_Q)),
        }
        try:
            out["temperature_c"] = self.temperature()
        except Exception as e:                        # noqa: BLE001
            out["temperature_error"] = f"{type(e).__name__}: {e}"
        out["tracked"] = {"range_v": self._range, "nplc": self._nplc}
        return out

    def query(self, cmd: str) -> str:
        """直接发白名单查询命令（外部不要拿它绕过白名单，见 commands.py）。"""
        return self._t().query(cmd)

    def write(self, cmd: str) -> None:
        """直接发白名单命令（同上）。"""
        self._t().write(cmd)

    @property
    def current_range(self) -> Optional[float]:
        """本会话**已下发过**的档位；None = 本会话没设过。

        3458A 白名单里**没有档位回读命令**（无 `DCV?`/`RANGE?`），所以这不是从设备
        读回来的值，只是"本会话设过什么"的记录——如实报告，别当成实测。
        """
        return self._range

    @property
    def current_nplc(self) -> Optional[float]:
        """本会话**已下发过**的 NPLC；None = 本会话没设过（同 `current_range` 口径）。"""
        return self._nplc

    # ---------- 会话恢复 / 重置 ----------
    def recover(self) -> None:
        """把表从"上次会话留下的现场"救回来：**IFC** → clear → 有限 drain → 挂起触发。

        现场教训（[SICL] L273-276 + 2026-09-23 事故）：上次会话若把 3458A 留在 free-run
        或 **Talk Only（ADDRESS 31，只讲不听）**，它会持续吐读数、不理查询；此时
        **先 drain 只会一直读到数据**（实测 91 s），而 Device Clear 也送不进去——
        必须先用 **IFC** 从总线层打断它。

        顺序**逐条移植参考项目** `dmm_sicl.py::open()`（`igpibpulseifc` → `clear`）：
        IFC → sleep 0.3 → clear → sleep 0.2 → 有界 drain → `TARM HOLD`/`TRIG HOLD`。

        ⚠ **成本**（2026-09-23 实测）：VISA 的 `VI_ATTR_TMO_VALUE` 有 ≈2 s 的最小粒度，
        250 ms 设不下去 → 每轮 drain 实际 ≈2 s。所以本函数是**慢路径**（≈5 s），
        只在 `connect(recover=True)` 或读数失败重试时走；默认 `recover="auto"` 不预做。

        ⚠ 如果连 IFC+clear 都停不下来（每条命令仍回读数），说明是 Talk Only，
        要改用 `unstick()`（多一步 `RESET`）。
        """
        transport = self._t()
        transport.ifc()                  # ← 真 IFC：VISA 不行自动回退 SICL（见 transport 注释）
        time.sleep(0.3)
        transport.clear()
        time.sleep(0.2)
        transport.drain(2, DRAIN_TIMEOUT_MS)
        transport.write(C.TARM_HOLD)     # 断言：后续不再自动触发
        transport.write(C.TRIG_HOLD)
        transport.drain(2, DRAIN_TIMEOUT_MS)

    def unstick(self) -> dict:
        """**救砖序列**（照参考项目 `dmm_sicl.py::open()` 移植 + 补验证）：

            IFC → clear → TARM/TRIG HOLD → `RESET` → clear → END ALWAYS/INBUF ON → 读 ID?

        什么时候用：表被留在 **Talk Only（前面板 ADDRESS=31，只讲不听）** 时——表现为
        **每条命令都"回"一个电压读数**、`viWrite` 超时；此时 `recover()` 停不下来
        （它在讲、不听），必须 **IFC 打断 + `RESET` 退出**。手册 p.159：
        "To remove the multimeter from Talk Only mode, press the Reset key or specify
        an address other than 31"。

        ⚠ **破坏性**：`RESET` 回到开机测量配置（档位/NPLC/功能/触发等，手册 p.26 Table 5）。
        返回 `{"unstuck": True, "idn": "HP3458A"|..., "voltage": ...}`。
        """
        transport = self._t()
        transport.ifc()
        ifc_path = getattr(transport, "ifc_path", None)
        time.sleep(0.3)
        transport.clear()
        time.sleep(0.3)
        transport.write(C.TARM_HOLD)
        transport.write(C.TRIG_HOLD)
        transport.write(C.RESET)
        time.sleep(1.2)                              # RESET 后稳定（参考实现 1.0~1.2 s）
        transport.clear()
        time.sleep(0.3)
        transport.write(C.END_ALWAYS)
        transport.write(C.INBUF_ON)
        transport.write(C.TRIG_AUTO)
        transport.drain(2, DRAIN_TIMEOUT_MS)
        self._range = None                           # RESET 复位了档位/NPLC → 记录作废
        self._nplc = None
        out: dict = {"unstuck": True,
                     "ifc_path": ifc_path,
                     "ifc_status": getattr(transport, "ifc_status", None),
                     "ifc_detail": getattr(transport, "ifc_detail", None),
                     "note": "IFC + clear + RESET（退出 Talk Only / 停 free-run）；"
                             "设备已回到开机测量配置。若 idn 仍是读数，说明表处于 "
                             "Talk Only（前面板 ADDRESS=31）——手册 p.159：只能前面板 "
                             "按 Reset 或把地址改成 31 以外的值"}
        try:
            out["idn"] = self.idn()
        except Exception as e:                        # noqa: BLE001
            out["idn_error"] = f"{type(e).__name__}: {e}"
        try:
            out["error"] = self.error_string()
        except Exception:                             # noqa: BLE001
            pass
        try:
            out["voltage"] = self.read_dcv()
        except Exception as e:                        # noqa: BLE001
            out["voltage_error"] = f"{type(e).__name__}: {e}"
        return out

    def reset(self) -> None:
        """`RESET` + `END ALWAYS` + `INBUF ON`——**回到开机测量配置（破坏性）**。

        会重置档位/NPLC/测量功能/内存/触发等（完整影响范围**待手册核对**，见
        docs/COMMANDS_3458A.md）。调用方必须先取得授权：MCP 的 `ks3458a_reset`
        要求 `confirm=True`，库层不做二次确认。

        `END ALWAYS`（每次读数置 EOI）与 `INBUF ON`（打开输入缓冲）是 `TARM SGL`
        单次读数配方的前置条件——缺了会把 GPIB 总线占住（[VISA] L571-576）。
        """
        transport = self._t()
        transport.write(C.TARM_HOLD)     # 先挂住触发，免得 RESET 与在跑的序列打架
        transport.write(C.TRIG_HOLD)
        transport.write(C.RESET)
        time.sleep(1.0)                  # RESET 后自校准/稳定（参考实现实测值）
        transport.clear()                # 中止 RESET 期间可能残留的序列
        time.sleep(0.3)
        transport.drain(DRAIN_MAX_ROUNDS, DRAIN_TIMEOUT_MS)
        transport.write(C.END_ALWAYS)
        transport.write(C.INBUF_ON)
        time.sleep(0.2)
        transport.drain(DRAIN_MAX_ROUNDS, DRAIN_TIMEOUT_MS)
        self._range = None               # RESET 已把档位/NPLC 复位 → 本会话记录作废
        self._nplc = None

    # ---------- 配置 ----------
    def _discard_first_reading(self) -> None:
        """丢弃换档/换配置后的第一次读数（建立时间 + 自校准，[SICL] L339-341）。

        失败不抛：这一步的目的是把过渡读数从管道里拿走，不是取数。
        """
        try:
            self.read_dcv()
        except Exception:                            # noqa: BLE001
            pass

    def configure_dcv(self, dcv_range=10.0, nplc: float = 10.0) -> None:
        """设直流档位与积分时间（`DCV <range>` / `NPLC <n>`），并丢弃首读数。

        `dcv_range` 传数值 = **固定档**（0.1/1/10/100/1000 V）；传 `"AUTO"` = **自动挡**
        （写 `DCV AUTO`，[MANUAL] p.184：max_input=AUTO → autorange）。
        """
        value = _check_range(dcv_range)
        transport = self._t()
        if value == "AUTO":
            transport.write(C.DCV_AUTO)
        else:
            transport.write(f"{C.DCV} {C.fmt_num(value)}")
        transport.write(f"{C.NPLC} {C.fmt_num(_check_nplc(nplc))}")
        self.dcv_range = value
        self.nplc = float(nplc)
        self._range = value
        self._nplc = float(nplc)
        time.sleep(0.3)
        self._discard_first_reading()

    def set_autorange(self, on: bool = True) -> None:
        """单独开关自动挡（`ARANGE ON` / `ARANGE OFF`，[MANUAL] p.160-161）。

        与 `configure_dcv("AUTO")` 的区别：这条**只动自动挡开关**、不碰档位数值/NPLC。
        `ARANGE ONCE`（p.53-54：让自动挡选一次档位）未封装——需要时按白名单单独发。
        """
        transport = self._t()
        transport.write(C.ARANGE_ON if on else C.ARANGE_OFF)
        self._autorange = bool(on)
        time.sleep(0.2)

    # ---------- 前板交接（snapshot / release / restore） ----------

    def snapshot_config(self) -> dict:
        """**测量配置**快照（FUNC/RANGE/NPLC/ARANGE/AZERO——不含触发态）。

        用途：`release_to_front_panel()` / `restore_config()` 配对——脚本开始前快照，
        结束后按快照还原**测量配置**，再把触发态无条件设回 AUTO（面板持续刷新）。

        为什么**不含** TARM/TRIG：触发态决定"面板是否刷新"，属于交接态而非用户
        设置——开始前若是上次烂尾的 HOLD，恢复回去等于把坏状态永久化（2026-10-08
        现场正是这样）。两类状态的处置见 `release_to_front_panel` 的设计说明。
        """
        t = self._t()

        def q(cmd: str):
            try:
                return t.query(cmd)
            except Exception:                        # noqa: BLE001
                return None

        def _num(raw, conv=float):
            if raw is None:
                return None
            try:
                return conv(str(raw).strip())
            except Exception:                        # noqa: BLE001
                return None

        func_raw = q(C.FUNC_Q)
        return {
            "function": func_raw,                    # '1, .1' = DCV + max_input
            "range_v": _num(q(C.RANGE_Q)),
            "nplc": _num(q(C.NPLC_Q)),
            "arange": _num(q(C.ARANGE_Q), int),      # 1=ON / 0=OFF（只信这条，见 state()）
            "azero": _num(q(C.AZERO_Q), int),        # 0=OFF / 1=ON / 2=ONCE
        }

    def restore_config(self, snap: dict, *, verify: bool = True) -> list[str]:
        """按 `snapshot_config()` 的快照还原**测量配置**（失败项记入返回的 issues 列表）。

        触发态不在还原范围（保持当前值）——需要面板刷新用 `release_to_front_panel()`。
        `None` 字段跳过（快照时该项查询失败，宁可不写也不猜）。
        """
        if not isinstance(snap, dict):
            raise ValueError("restore_config 需要 snapshot_config() 返回的 dict")
        t = self._t()
        issues: list[str] = []
        if snap.get("range_v") is not None:
            t.write(f"{C.DCV} {C.fmt_num(float(snap['range_v']))}")
        if snap.get("nplc") is not None:
            t.write(f"{C.NPLC} {C.fmt_num(float(snap['nplc']))}")
        if snap.get("azero") is not None:
            az = int(snap["azero"])
            t.write(f"AZERO {'ON' if az == 1 else 'OFF'}")   # ONCE(2) 不还原（一次性动作）
        if snap.get("arange") is not None:
            t.write(C.ARANGE_ON if int(snap["arange"]) == 1 else C.ARANGE_OFF)
        time.sleep(0.3)
        if verify:
            after = self.snapshot_config()
            for key in ("range_v", "nplc", "arange"):
                want, got = snap.get(key), after.get(key)
                if want is not None and got is not None and abs(float(want) - float(got)) > 1e-9:
                    issues.append(f"{key}: 期望 {want} 实测 {got}")
        self._range = snap.get("range_v")
        self._nplc = snap.get("nplc")
        return issues

    def release_to_front_panel(self, *, arange: bool = True) -> list[str]:
        """把仪器交还前面板：触发态无条件回 AUTO（面板连续刷新），ARANGE 可选。

        设计裁定（2026-10-08，治"测完面板停在 MRNG+冻结读数"）：
        * **触发态（TARM/TRIG）无条件 AUTO**——它决定面板死活；开始前若是 HOLD，
          "按开始状态恢复"会把坏状态永久化，所以触发态不属于快照范围；
        * **ARANGE 默认 ON**：MRNG（manual range）指示符只在非自动挡时点亮——
          多数场景用户要的是"面板正常显示"，固定档属于特意配置，由
          snapshot/restore_config 那条链负责，不在这里。
        * 返回 issues（ARANGE 回读不符等），**不因面板态问题抛错**——收尾尽力而为。

        发送序列：`TARM AUTO` + `TRIG AUTO`（+ 可选 `ARANGE ON`）。
        注意：发完**不要**再调 `read_dcv()`——单次触发会把 TARM 打回 HOLD。
        """
        t = self._t()
        t.write(C.TARM_AUTO)
        t.write(C.TRIG_AUTO)
        if arange:
            t.write(C.ARANGE_ON)
        time.sleep(0.2)
        issues: list[str] = []
        try:
            st = self.state()
            if str(st.get("tarm", "")).startswith("4"):
                issues.append(f"tarm 仍 HOLD: {st.get('tarm')}")
            if str(st.get("trig", "")).startswith("4"):
                issues.append(f"trig 仍 HOLD: {st.get('trig')}")
            if arange and str(st.get("arange", "")).startswith("0"):
                issues.append(f"arange 仍 OFF: {st.get('arange')}")
        except Exception as e:                        # noqa: BLE001 —— 回读失败只记录
            issues.append(f"回读失败: {type(e).__name__}: {e}")
        return issues

    def set_nplc(self, nplc: float) -> None:
        """只改积分时间（`NPLC <n>`），不动档位。

        与 `configure_dcv` 不同，这里**不丢弃首读数**（参考实现同样如此，
        [SICL] L353-357）：NPLC 变化不引起换档建立过程。要绝对干净请自己先读一次丢。

        ⚠ **交流功能下 NPLC 的语义未核对**（`SETACV SYNC` 的积分含义与 DCV 不同）。
        """
        value = _check_nplc(nplc)
        self._t().write(f"{C.NPLC} {C.fmt_num(value)}")
        self.nplc = value
        self._nplc = value
        time.sleep(0.3)

    def set_range(self, dcv_range) -> None:
        """切换直流档位（数值=固定档 / `'AUTO'`=自动挡）；**已在该档位则不重发**。

        10V 档物理上可到 12 V（[MANUAL] p.136/p.173：120% of range），但本库按 1.1 倍
        余量选档（保守）；确知信号 ≤12 V 又要用 10V 档时直接 `set_range(10.0)`。
        """
        value = _check_range(dcv_range)
        if self._range == value:
            return
        if value == "AUTO":
            self._t().write(C.DCV_AUTO)
        else:
            self._t().write(f"{C.DCV} {C.fmt_num(value)}")
        self._range = value
        time.sleep(0.4)
        self._discard_first_reading()

    @staticmethod
    def range_for(volt: float) -> float:
        """给定电压选一个能覆盖它的**最小**档位（留 10% 余量，[VISA] L603-609）。"""
        need = abs(float(volt)) * 1.1
        for allowed in C.DCV_RANGES:
            if need <= allowed:
                return allowed
        return C.DCV_RANGES[-1]

    def configure_acv(self, acv_range: float = 10.0, band_lo: Optional[float] = None,
                      band_hi: Optional[float] = None, sync: bool = False) -> None:
        """设交流档位与转换方式（`ACV` / `SETACV` / `ACBAND`），并丢弃首读数。

        `sync=False` → `SETACV ANA`（模拟转换，默认）；`True` → `SETACV SYNC`（同步采样）。
        `band_lo`/`band_hi` 给定时下发 `ACBAND <lo>,<hi>`（Hz），**必须成对给**。

        ⚠ 这一组命令**未在参考实现与官方样例中出现**（出处 [TASK]），真机首跑前
        请对照手册；`SETACV` 的取值全集、`ACBAND` 的合法带宽、AC 下 NPLC 的语义
        都列在 docs/COMMANDS_3458A.md「待手册核对项」。
        """
        value = _check_range(acv_range)
        if (band_lo is None) != (band_hi is None):
            raise ValueError("ACBAND 需要同时给出下限与上限（Hz），不能只给一个")
        transport = self._t()
        transport.write(f"{C.ACV} {C.fmt_num(value)}")
        transport.write(C.SETACV_SYNC if sync else C.SETACV_ANA)
        if band_lo is not None:
            low, high = float(band_lo), float(band_hi)
            if not high > low:
                raise ValueError(f"ACBAND 上限必须大于下限：{low} Hz, {high} Hz")
            transport.write(f"{C.ACBAND} {C.fmt_num(low)},{C.fmt_num(high)}")
        self.acv_range = value
        self.acv_sync = bool(sync)
        time.sleep(0.3)
        self._discard_first_reading()

    # ---------- 读数 ----------
    def _read_single(self, what: str = "DCV") -> float:
        """`TARM SGL,1` 触发一次读数并回值（3458A 单次读数的标准配方）。

        响应非数值时**报错**（不返回 0 兜底）：常见成因是上次会话的残留/响应错位，
        静默兜底会把错位数据当读数用。响应里含多行（free-run 残留）时取**最后一个**
        token——参考实现同口径（`[SICL]`），而"取最后一个"这件事本身也是错位的信号，
        调用方 `_with_recover_retry` 会在失败时做恢复重试。
        """
        raw = self._t().query(C.TARM_SGL_1)
        token = raw.split()[-1] if raw.split() else ""
        try:
            return float(token)
        except ValueError as exc:
            raise TransportError(
                f"3458A 返回非法{what}读数: {raw!r}"
                f"（非数值——可能是残留响应/总线错位，先 recover() 或查 ERRSTR?）") from exc

    def read_dcv(self, timeout_s: Optional[float] = None) -> float:
        """单次直流电压读数（V）。`timeout_s` 可覆盖本次读取超时。

        `connect(recover="auto")` 下：直接读（快）；若超时/响应非法，自动做一次
        会话恢复并重试一次（`_with_recover_retry`）——把恢复成本只花在真正需要的场合。
        """
        if timeout_s is not None:
            self._t().set_timeout(timeout_s)
        try:
            return self._with_recover_retry(lambda: self._read_single("DCV"))
        finally:
            if timeout_s is not None:
                self._t().set_timeout(self.timeout_s)

    def read_acv(self, timeout_s: Optional[float] = None) -> float:
        """单次交流电压读数（V）。配方同 `read_dcv`（`TARM SGL,1`）。

        AC 单次读数的配方**手册未直接给出**（属于本项目待实测项）；本方法沿用 `TARM SGL,1`
        并同样带"失败→恢复→重试一次"。实测结论见
        `docs/3458a_manual_verification_20260923.md` 与 `TEST_SCRIPTS/ks3458a/`。
        """
        if timeout_s is not None:
            self._t().set_timeout(timeout_s)
        try:
            return self._with_recover_retry(lambda: self._read_single("ACV"))
        finally:
            if timeout_s is not None:
                self._t().set_timeout(self.timeout_s)

    def read_avg(self, n: int = 1) -> float:
        """连续读 n 次取平均（降噪）。n≤1 等同单次读数。"""
        count = int(n)
        if count < 1:
            raise ValueError(f"n 必须 ≥1，收到 {n!r}")
        if count == 1:
            return self.read_dcv()
        return sum(self.read_dcv() for _ in range(count)) / count

    def read_stats(self, n: int = 1) -> dict:
        """连续读 n 次，返回 `{n, mean, stddev, min, max}`（样本标准差）。"""
        count = int(n)
        if count < 1:
            raise ValueError(f"n 必须 ≥1，收到 {n!r}")
        return _stats([self.read_dcv() for _ in range(count)])

    def read_series(self, n: int = 10, interval_s: Optional[float] = None,
                    timeout_s: Optional[float] = None) -> dict:
        """**一次会话内连续重复测量 n 次**（脚本/批量采集的主用接口）。

        与 `read_avg`/`read_stats` 的区别：本方法返回**逐点序列 + 时间戳 + 时长**，
        可选点间间隔（`interval_s`，用主机 sleep，非精密时序）；与 MCP 逐次调用相比
        省掉了每次重连（快路径 0.27 s/次）——100 点从 ~70 s 降到 ~45 s（NPLC 10）。

        返回 `{"values": [...], "summary": {n, mean, stddev, min, max, duration_s,
        interval_s, per_reading_s, nplc, stopped_early}}`。
        单点失败会重试一次（`_with_recover_retry`）；仍失败则**带上已采到的点**报错，
        不静默丢数据。
        """
        count = int(n)
        if count < 1:
            raise ValueError(f"n 必须 ≥1，收到 {n!r}")
        gap = float(interval_s) if interval_s else 0.0
        values: list[float] = []
        stamps: list[float] = []
        t0 = time.time()
        for i in range(count):
            if i and gap > 0:
                time.sleep(gap)
            try:
                if timeout_s is not None:
                    v = self.read_dcv(timeout_s=timeout_s)
                else:
                    v = self.read_dcv()
            except Exception as exc:                      # noqa: BLE001
                raise TransportError(
                    f"连续测量在第 {i + 1}/{count} 点失败（已成功 {len(values)} 点）："
                    f"{type(exc).__name__}: {exc}") from exc
            values.append(float(v))
            stamps.append(time.time())
        total = time.time() - t0
        summary = _stats(values)
        summary.update({
            "duration_s": round(total, 4),
            "interval_s": gap or None,
            "per_reading_s": round(total / count, 4) if count else None,
            "nplc": self._nplc,
            "stopped_early": False,
        })
        return {"values": values, "timestamps": stamps, "summary": summary}

    # ---------- 高速二进制突发 ----------
    def read_burst(self, n: int, sample_interval_s: Optional[float] = None,
                   dcv_range: Optional[float] = None,
                   aperture_s: Optional[float] = None,
                   timeout_s: Optional[float] = None,
                   data_format: str = "SINT", restore: bool = True) -> dict:
        """高速采样：一次触发取 n 个读数（Keysight 官方 100k rdg/s 配方）。

        下发序列（SINT，逐条对应 [SAMPLE] L43-69）::

            PRESET DIG → [DCV <range>] → MFORMAT SINT → OFORMAT SINT
            → [APER <s>] → [TIMER <s>] → MEM OFF → NRDGS n → TRIG AUTO
            → ISCALE? → TARM SYN → 读 2n+2 字节

        `data_format="DINT"` 时改用 `MFORMAT/OFORMAT DINT`（4 字节/读数）并读 `4n` 字节。
        **为什么要有 DINT**：[MANUAL] p.173 —— direct-sampling 下 DINT 的满量程是档位的
        **500%**，而 SINT 只有约 120%；超过 120% 档位的信号必须用 DINT，否则溢出。
        DINT 没有官方样例的"尾部 2 字节"约定，故按 `4n` 精确读取后再做**有界** drain。

        解析：SINT = 2 字节大端有符号整数 × ISCALE；DINT = 4 字节大端有符号整数 × ISCALE。
        返回 `{"values": [...], "summary": {...}}`。

        超时/字节数不足会明确报错（`TransportError`），**不返回截断数据**。
        """
        count = int(n)
        if count < 1:
            raise ValueError(f"n 必须 ≥1，收到 {n!r}")
        if count > C.BURST_MAX_READINGS:
            raise ValueError(
                f"n={count} 超过设备上限 {C.BURST_MAX_READINGS}"
                f"（[MANUAL] p.207：NRDGS 的 n 合法范围 1..16777215）")
        fmt = str(data_format).strip().upper()
        if fmt not in ("SINT", "DINT"):
            raise ValueError(f"data_format 只支持 'SINT' / 'DINT'，收到 {data_format!r}")
        if dcv_range is not None:
            dcv_range = _check_range(dcv_range)
            if dcv_range == "AUTO":
                raise ValueError("突发必须用固定档位（先 set_range(<数值>)，自动挡不适用）")
        per_reading = 2 if fmt == "SINT" else C.DINT_BYTES
        trailer = 2 if fmt == "SINT" else 0        # 尾部 2 字节只存在于官方 SINT 样例
        transport = self._t()
        want = per_reading * count + trailer

        def safe_write(cmd: str) -> None:
            """写一条命令；失败（多半是表还在流数据）→ 恢复一次 → 重试一次。

            现场教训：表被上次会话留在连续输出时，`PRESET DIG` 直接 `VI_ERROR_TMO`；
            旧实现只会抛错，把设备留在原地（后续调用继续撞）。现在自动救一次。
            """
            try:
                transport.write(cmd)
            except TransportError:
                self.recover()
                transport.write(cmd)

        try:
            safe_write(C.PRESET_DIG)
            if dcv_range is not None:
                safe_write(f"{C.DCV} {C.fmt_num(dcv_range)}")
                self._range = dcv_range
            safe_write(C.MFORMAT_SINT if fmt == "SINT" else C.MFORMAT_DINT)
            safe_write(C.OFORMAT_SINT if fmt == "SINT" else C.OFORMAT_DINT)
            if aperture_s is not None:
                safe_write(f"{C.APER} {C.fmt_num(aperture_s)}")
            effective_interval = sample_interval_s
            if sample_interval_s is not None:
                safe_write(f"{C.TIMER} {C.fmt_num(sample_interval_s)}")
            else:
                # 没给采样间隔就用设备当前 TIMER；顺手查出来算超时预算
                try:
                    effective_interval = _first_float(transport.query(C.TIMER_Q))
                except Exception:                    # noqa: BLE001
                    effective_interval = C.DEFAULT_SAMPLE_INTERVAL_S
            safe_write(C.MEM_OFF)
            safe_write(f"{C.NRDGS} {count}")
            safe_write(C.TRIG_AUTO)
            iscale = _first_float(transport.query(C.ISCALE_Q))
            safe_write(C.TARM_SYN)

            # 超时预算按"n × 采样间隔"算，而不是吃通用的 120 s——否则一旦设备状态
            # 不对（例如格式/触发没生效），调用会长时间占着设备锁把 MCP 堵死。
            if timeout_s is None:
                interval = float(effective_interval or C.DEFAULT_SAMPLE_INTERVAL_S)
                timeout_s = min(C.BURST_TIMEOUT_MAX_S,
                                max(C.BURST_TIMEOUT_MIN_S, 10.0 + 3.0 * count * interval))
            raw = transport.read_bytes(want, timeout_s=timeout_s)
            # 传输层已保证字节数；这里再挡一道——截断数据会解析出错位的"合理值"，
            # 比直接报错危险得多（宁可失败，不可静默给错数）
            if len(raw) < want:
                raise TransportError(
                    f"二进制突发回读不足：期望 {want} 字节，实得 {len(raw)} 字节"
                    f"（n={count}, {fmt}；超时或设备未按 {fmt} 配方输出）")
        finally:
            # **无论成功失败都要收尾 + 恢复**：
            # ① 收尾：`TRIG AUTO`+`NRDGS n`+`MEM OFF` 之下表取满后仍会继续触发/输出，
            #    不收尾下一条命令必撞 `VI_ERROR_TMO`（2026-09-23 实测）；
            # ② 恢复：`PRESET DIG` 把输出留在 **SINT**（无换行符），此后 ASCII 读数
            #    会超时（现场实测：MCP 里 burst 之后整个工具面瘫掉）。
            #    手册 p.217 的 `PRESET NORM` 是官方"退出数字档"路径（**不是 RESET**）。
            try:
                transport.clear()
                transport.write(C.TARM_HOLD)
                transport.write(C.TRIG_HOLD)
                transport.drain(2, DRAIN_TIMEOUT_MS)
            except Exception:                        # noqa: BLE001 —— 收尾尽力而为
                pass
            if restore:
                try:
                    transport.write(C.PRESET_NORM)
                    time.sleep(1.0)                  # 预设切换稳定时间（与 RESET 同级）
                    transport.clear()
                    transport.write(C.END_ALWAYS)
                    transport.write(C.INBUF_ON)
                    transport.write(C.TRIG_AUTO)
                    transport.drain(2, DRAIN_TIMEOUT_MS)
                except Exception:                    # noqa: BLE001
                    pass
                # PRESET NORM 会把档位/NPLC 复位（与 RESET 同级）→ 本会话记录作废
                self._range = None
                self._nplc = None

        values = [int.from_bytes(raw[i:i + per_reading], "big", signed=True) * iscale
                  for i in range(0, per_reading * count, per_reading)]
        summary = _stats(values)
        summary.update({
            "data_format": fmt,
            "iscale_v_per_lsb": iscale,
            "bytes_read": len(raw),
            "bytes_expected": want,
            "dcv_range": dcv_range if dcv_range is not None else self._range,
            "sample_interval_s": sample_interval_s,
            "effective_interval_s": effective_interval,
            "read_timeout_s": timeout_s,
            "aperture_s": aperture_s,
            "restored_to_preset_norm": bool(restore),
        })
        return {"values": values, "summary": summary}
