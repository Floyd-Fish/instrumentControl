"""操作目录——每个仪器操作的**声明式**元数据（设备族 / 风险等级 / 确认要求 / 验证项）。

这是方案 C 阶段 1 引入的新事实源。它只承载「判断与分类」，不重复承载
schema 与描述——那两样由 FastMCP 从函数签名与 docstring 生成后原样传入
（见 registry.py 顶部说明）。

表中的数据来源（不是凭印象填的，逐项有出处）：
  * requires_confirm：由 68 个工具的 JSON Schema 实测得出——恰好 10 个工具声明了
    `confirm` 参数（sds_shutdown 还是 required），见 TEST_DATA 的 tools 快照。
  * verify 的 readback：只对**文档明确写了"回读/写后回读"**的工具标注
    （在 68 个 description 里检索"回读"得到 14 个，去掉只读工具后取交集）。
  * raw_scpi：三个把任意 SCPI 字符串送到设备的通道（dg_query / instr_query /
    instr_write），代码里都先过 `_is_query_only` 与 `_is_forbidden`——见
    mcp_instruments/server.py 对应函数体。
  * risk：按"是否改变设备状态、是否不可逆"判定；只读类工具不改变任何状态。

新增工具时**必须**在此登记，否则 `verify_registry_parity.py` 会报覆盖缺口
（这是刻意的：宁可测试失败，也不要一个没被分类的工具悄悄上线）。
"""
from __future__ import annotations

from typing import Iterable

from .registry import Safety

__all__ = [
    "DEVICE_BY_PREFIX",
    "RISK_BY_TOOL",
    "CONFIRM_TOOLS",
    "RAW_SCPI_TOOLS",
    "VERIFY_BY_TOOL",
    "NOTES_BY_TOOL",
    "device_for_tool",
    "safety_for_tool",
    "unclassified",
]

#: 工具名前缀 -> 设备族键。canonical id 的前半段也取自这里（`sdg.set_wave`）。
DEVICE_BY_PREFIX: dict[str, str] = {
    "dg": "dg832",
    "sds": "sds",
    "sdg": "sdg",
    "dmm": "dmm",
    "dho": "dho",
    "mho": "mho",
    "psu": "psu",
    "ks3458a": "ks3458a",
    "instr": "instr",
    "usb": "usb",
}

_READ_ONLY = "read_only"
_CONFIG = "config"
_OUTPUT = "output"
_DESTRUCTIVE = "destructive"

#: 68 个操作的风险等级（全部显式登记）。
RISK_BY_TOOL: dict[str, str] = {
    # ---- DG832 信号源（12）----
    "dg_status": _READ_ONLY,
    "dg_get_protect": _READ_ONLY,
    "dg_check_error": _READ_ONLY,
    "dg_counter": _READ_ONLY,
    "dg_query": _READ_ONLY,          # 纯查询通道，但走原始 SCPI（见 RAW_SCPI_TOOLS）
    "dg_protect": _CONFIG,           # 设电压保护（开幅度/输出前的前置条件）
    "dg_set_wave": _CONFIG,
    "dg_set_param": _CONFIG,
    "dg_set_dc": _CONFIG,
    "dg_sweep": _CONFIG,
    "dg_sweep_trigger": _CONFIG,
    "dg_output": _OUTPUT,            # 改变输出开关（可能打断在途测试）
    # ---- SDS 示波器（13）----
    "sds_status": _READ_ONLY,
    "sds_diagnose": _READ_ONLY,
    "sds_measure": _READ_ONLY,
    "sds_measure_phase": _READ_ONLY,
    "sds_get_waveform": _READ_ONLY,
    "sds_screenshot": _READ_ONLY,
    "sds_meas_threshold": _CONFIG,
    "sds_meas_gate": _CONFIG,
    "sds_meas_statistics": _CONFIG,
    "sds_meas_dtime": _CONFIG,
    "sds_meas_display": _CONFIG,
    "sds_auto_scale": _CONFIG,
    "sds_shutdown": _DESTRUCTIVE,    # 远程关机，需面板手动开机
    # ---- SDG 信号源（4）----
    "sdg_status": _READ_ONLY,
    "sdg_counter": _READ_ONLY,
    "sdg_set_wave": _CONFIG,
    "sdg_output": _OUTPUT,
    # ---- Keysight 3446x 万用表（4）----
    "dmm_status": _READ_ONLY,
    "dmm_measure": _READ_ONLY,
    "dmm_configure": _CONFIG,
    "dmm_nplc": _CONFIG,
    # ---- RIGOL DHO 示波器（5，不在本实验台）----
    "dho_status": _READ_ONLY,
    "dho_measure_item": _READ_ONLY,
    "dho_channel": _CONFIG,
    "dho_timebase": _CONFIG,
    "dho_trigger": _CONFIG,
    # ---- RIGOL MHO900 示波器（10）----
    "mho_status": _READ_ONLY,
    "mho_measure_item": _READ_ONLY,
    "mho_screenshot": _READ_ONLY,
    "mho_get_waveform": _READ_ONLY,
    "mho_channel": _CONFIG,
    "mho_timebase": _CONFIG,
    "mho_trigger": _CONFIG,
    "mho_fit_channel": _CONFIG,      # 单通道自动定标（改档位/偏置）
    "mho_acquisition": _CONFIG,      # run/stop/single 改变采集状态
    "mho_autoset": _CONFIG,          # 全局破坏性 autoset
    # ---- DH1766 电源（5）----
    "psu_status": _READ_ONLY,
    "psu_mode": _READ_ONLY,
    "psu_set_mode": _CONFIG,
    "psu_output": _OUTPUT,
    "psu_power_cycle": _OUTPUT,
    # ---- HP/Keysight 3458A 万用表（11）----
    "ks3458a_status": _READ_ONLY,
    "ks3458a_read": _READ_ONLY,
    "ks3458a_read_avg": _READ_ONLY,
    "ks3458a_read_series": _READ_ONLY,
    "ks3458a_read_stats": _READ_ONLY,
    "ks3458a_burst": _READ_ONLY,
    "ks3458a_configure": _CONFIG,
    "ks3458a_autorange": _CONFIG,
    "ks3458a_acv": _CONFIG,
    "ks3458a_reset": _DESTRUCTIVE,
    "ks3458a_unstick": _DESTRUCTIVE,  # 真实 IFC 复位，解 GPIB 卡死
    # ---- 通用零代码接入（3）----
    "instr_discover": _READ_ONLY,
    "instr_query": _READ_ONLY,       # 原始 SCPI 查询通道
    "instr_write": _DESTRUCTIVE,     # 原始 SCPI 写通道（黑名单 + 回读）
    # ---- 故障兜底（1）----
    "usb_reset": _DESTRUCTIVE,       # 重启 USB PnP 节点，会中断该仪器所有会话
}

#: 声明了 `confirm` 参数的 10 个工具（实测自 tools 快照）。
#: server.py 登记时会用函数签名复核，不一致直接报错——两处打架不允许。
CONFIRM_TOOLS: frozenset[str] = frozenset({
    "dg_output",
    "sdg_output",
    "sds_shutdown",
    "mho_autoset",
    "psu_output",
    "psu_power_cycle",
    "ks3458a_reset",
    "ks3458a_unstick",
    "instr_write",
    "usb_reset",
})

#: 会把**任意 SCPI 字符串**送到设备的通道——`_is_forbidden` / `_is_query_only` 的适用面。
RAW_SCPI_TOOLS: frozenset[str] = frozenset({
    "dg_query",
    "instr_query",
    "instr_write",
})

#: 执行后应做的验证。readback 只标在文档明确写了"回读"的工具上。
VERIFY_BY_TOOL: dict[str, tuple[str, ...]] = {
    "dho_channel": ("readback",),
    "dho_timebase": ("readback",),
    "dho_trigger": ("readback",),
    "dmm_configure": ("readback",),
    "dmm_nplc": ("readback",),
    "ks3458a_configure": ("readback",),
    "mho_acquisition": ("readback",),
    "mho_channel": ("readback",),
    "mho_timebase": ("readback",),
    "mho_trigger": ("readback",),
    "psu_set_mode": ("readback",),
    "sdg_set_wave": ("readback",),
    "instr_write": ("readback", "error_queue"),
    "instr_query": ("error_queue",),
}

#: 影响"能否安全调用"的关键约束，供 compact profile 的 describe() 输出。
#: 只放**会改变行动路径**的语义，不放排障叙事（那是 description 的职责）。
NOTES_BY_TOOL: dict[str, str] = {
    "dg_protect": "设幅度/偏移或开输出之前必须先开启有效保护（state=True 且 high>low），"
                  "否则被库内联锁拒绝（protect_required）。",
    "dg_output": "开/关都需 confirm=True。开启前需已有有效电压保护。",
    "dg_set_wave": "设 amp/offset 前需已开保护，否则 protect_required；越界返回 protect_range。",
    "dg_set_dc": "切回非 DC 波形时须用返回的 restore 值显式传参，库不做隐式恢复。",
    "psu_output": "开/关都需 confirm=True，且 expect_mode 必须与实际模式一致（防拓扑误判）。",
    "psu_power_cycle": "需 confirm=True；下电放电时间不足时调大 off_delay_s。",
    "psu_set_mode": "切换前所有输出必须关闭，否则直接拒绝（继电器联动拓扑变化）。",
    "sdg_output": "开/关都需 confirm=True；expect_load 必须与实际负载设置一致"
                  "（HZ 高阻 / 50 为 50Ω，幅度语义随负载不同）。",
    "sds_shutdown": "破坏性：设备离线后需面板手动开机，confirm 为必填参数。",
    "usb_reset": "只支持 USB 资源；改设备节点需管理员权限，MCP 进程无法自行提权。",
    "mho_autoset": "全局破坏性：会重调所有通道档位、时基与触发，多信号实验台上勿用。",
    "mho_acquisition": "stop 会冻结采集；共享实验台上可能打断他人观察。",
    "ks3458a_reset": "破坏性复位，需 confirm=True。",
    "ks3458a_unstick": "真实 IFC 复位，用于解 GPIB 卡死；会中断该仪器所有会话。",
    "instr_write": "原始 SCPI 写通道：复位/存储覆写类一律拒绝（confirm 也不放行）；"
                   "写前排空错误队列、写后回读并返回 syst_errors。",
    "instr_query": "原始 SCPI 查询通道：每条 `;` 分段都必须是查询（问号后允许带参数）。",
    "dg_query": "DG832 原始 SCPI 查询通道：判据同 instr_query，复位/锁定类一律拒绝。",
}


def device_for_tool(tool_name: str) -> str:
    """按前缀取设备族键；未知前缀抛错（逼新增工具显式登记）。"""
    prefix = tool_name.split("_", 1)[0]
    try:
        return DEVICE_BY_PREFIX[prefix]
    except KeyError:
        raise KeyError(
            f"unknown device prefix {prefix!r} for tool {tool_name!r}; "
            f"register it in DEVICE_BY_PREFIX"
        ) from None


def safety_for_tool(tool_name: str, *, requires_confirm: bool | None = None) -> Safety:
    """取某工具的安全属性。

    requires_confirm 传入**函数签名实测值**时做一致性复核：目录说"要确认"而签名里
    没有 confirm 参数（或反之）都会直接抛错——这类漂移必须在登记时就炸，而不是等
    真实硬件操作时才发现少了一道门。
    """
    try:
        risk = RISK_BY_TOOL[tool_name]
    except KeyError:
        raise KeyError(
            f"tool {tool_name!r} is not classified in RISK_BY_TOOL; "
            f"every tool must be explicitly classified"
        ) from None

    expected = tool_name in CONFIRM_TOOLS
    if requires_confirm is not None and requires_confirm != expected:
        raise ValueError(
            f"confirm mismatch for {tool_name!r}: signature says requires_confirm="
            f"{requires_confirm}, catalog says {expected} (CONFIRM_TOOLS)"
        )

    return Safety(
        risk=risk,
        requires_confirm=expected,
        raw_scpi=tool_name in RAW_SCPI_TOOLS,
        verify=VERIFY_BY_TOOL.get(tool_name, ()),
        note=NOTES_BY_TOOL.get(tool_name, ""),
    )


def unclassified(tool_names: Iterable[str]) -> tuple[list[str], list[str]]:
    """返回 (目录里有但未注册的工具, 已注册但目录里没有的工具)。两者都应为空。"""
    known = set(RISK_BY_TOOL)
    given = set(tool_names)
    return sorted(known - given), sorted(given - known)
