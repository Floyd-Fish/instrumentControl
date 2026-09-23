# -*- coding: utf-8 -*-
"""精简 mcp_instruments/server.py 的 MCP 工具 docstring（= 模型看到的 tool description）。

背景：工具 description 常驻每次请求的固定前缀。2026-09-23 实测 68 个工具的
description 合计 9,358 est tok。其中大量内容是手册页码、实测数据、背景原理、
返回体字段清单、长参数枚举——这些对"决定调不调、能不能调"无用，只对"怎么填参数"
有用，而后者属于**使用时才需要的手册**。

判据（本脚本遵守）：
  保留 ① 能力一句话
       ② 安全/授权门（confirm / expect_* / 库内联锁 / 破坏性 / 未实机验证）
       ③ 短枚举（≤8 项，一行内说完，如 action/mode/slope/wvtp）
       ④ 关键返回判读（直接改变下一步决策的结论）
       ⑤ 指向 skill 的定位锚（章节名或工具名），保证可追溯
  移除 ① 手册页码 ② 实测数据与日期 ③ 背景原理与历史
       ④ 详细返回体字段清单 ⑤ 长参数枚举（item/shape 等 10 项以上）
       ⑥ 证据/设计文档路径与跨工具长对比

  移除项**不删信息**，全部已在 skill `instrument-mcp`
  （~/.agents/skills/instrument-mcp/SKILL.md，509 行超集）中保留；
  长枚举由本批次配套补进该 skill 的「参数语义速查」表。

用法：
    python TEST_SCRIPTS/common/trim_mcp_tool_docstrings_20260923.py --dry-run
    python TEST_SCRIPTS/common/trim_mcp_tool_docstrings_20260923.py --apply
"""
from __future__ import annotations

import argparse
import ast
import difflib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SERVER = ROOT / "mcp_instruments" / "server.py"

PATCHES: dict[str, str] = {}

PATCHES["instr_discover"] = """发现本机所有仪器（LAN 网段 + USB/GPIB/串口），结果自动回写地址缓存——
之后各专用工具不传 resource 也能连上。

cidr 可选；省略时扫本机所有接口所在的私网网段（多网卡/挂 VPN/代理 TUN 都能覆盖）。
换了网段、换了 USB 口/串口号，或专用工具报连接失败时，先重跑本工具。

返回体五组字段（lan / visa / resolved / recognised_now / psu_local_restored）
的语义见 skill instrument-mcp →「工具选择决策树」。"""

PATCHES["instr_query"] = """通用 SCPI 查询——新设备零代码接入。resource 必填（完整 VISA 资源串）；
cmd 必须含 '?'；timeout_ms 500-30000，默认 5000。只读不留痕；写操作用 instr_write。
**3458A 不是 SCPI 表，不要用它。**

护栏：每条 `;` 分段都必须是纯查询（命令头带 '?'，**问号后允许带参数**），
夹带写/复位/锁定则整条拒。串口(ASRL)按 9600 波特。
判据细节与看门狗见 skill instrument-mcp →「通用护栏工具」。"""

PATCHES["instr_write"] = """通用 SCPI 写——新设备零代码接入。⚠ 必须 confirm=True（raw 写权限大）。
**3458A 不是 SCPI 表，不要用它。**

护栏：复位/存储覆写类一律 forbidden（confirm 也不放行）；写前 drain、写后逐条
SYST:ERR?；readback_cmd 给定即自动回读（同样受黑名单约束）；每次调用含被拒均落盘
审计；看门狗超时记 executed="unknown"（命令可能已送达，须回读确认）。
返回 result: {written, pre_errors, syst_errors, readback}。
细节见 skill instrument-mcp →「通用护栏工具」。"""

PATCHES["usb_reset"] = """⚠ 故障兜底：重启某台仪器所占用的 USB PnP 设备节点（USB-TMC 卡死时用）。

用在哪：`*IDN?` 超时 / VI_ERROR_TMO / VI_ERROR_SYSTEM_ERROR，且**先重连一次仍
不恢复**时。效果：USB 重新枚举——**固件不重启、通道设定/输出/保护全部保留**
（实测 2.4s）；比拔插 USB 省事，**不要**给仪器断电。

参数：kind（sds/sdg/dmm/dho/mho/dg/psu）或 resource 二选一；confirm=True 必填
（该仪器所有会话中断 2~3 秒）；verify_idn=True 复位后轮询 *IDN? 确认。
仅支持 USB 资源——LAN 卡死请先重连/换协议。

⚠ 需管理员权限，MCP 进程不能自行提权、不弹 UAC——出路见 skill instrument-mcp
→「USB-TMC 卡死」。"""

PATCHES["ks3458a_status"] = """3458A 只读状态：`ID?` / `ERRSTR?` / `TEMP?`，外加 `device`（设备回读 =
真实配置）与 `tracked`（本会话下发过什么）——两者语义分开，别混。

⚠ 连接时会做**会话恢复**（IFC/Device Clear + 有限 drain + TARM/TRIG HOLD）
+ **读前准备**（END ALWAYS/INBUF ON/TRIG AUTO）：上次会话若把表留在 free-run
或 TRIG HOLD，不处理就会读到错位数据 / 超时。代价是可能打断同总线他人采集。

`device` 字段清单与码表解码见 skill instrument-mcp →「参数语义速查 · ks3458a_status」。"""

PATCHES["ks3458a_burst"] = """3458A 高速二进制突发（100k rdg/s）：一次触发取 n 个读数，返回**摘要**；
逐点数据用 save_csv=True。n 上限 16777215（越大越慢、内存越大）。

data_format="SINT"（默认，2 字节/读数）或 "DINT"（4 字节/读数）——
**信号可能超过档位 120% 时必须用 DINT**（SINT 满量程仅约该档位 120%，会溢出）。

⚠ **会改设备配置**：`PRESET DIG` 把整组采样参数复位到数字档。跑完本工具自动收尾并
`PRESET NORM` 恢复 ASCII 输出（**不是** RESET）；要还原自己的档位/NPLC 请显式
ks3458a_configure。sample_interval_s / dcv_range 省略则不下发（用设备当前值）。

完整配方与收尾契约见 skill instrument-mcp →「参数语义速查 · ks3458a_burst」。"""

PATCHES["ks3458a_unstick"] = """3458A **救砖**：`IFC` → clear → `TARM/TRIG HOLD` → `RESET` → clear →
`END ALWAYS`/`INBUF ON` → 读 `ID?`。

用于"表能听但一直吐数"（free-run）或全部调用超时、普通 Device Clear 停不下来时。
⚠ **对 Talk Only 无效**：表根本不听命令，IFC/RESET 都送不进去——那一态**只能面板**
（`Address` → `9` → `Enter`，或按 Reset 键）。先看/问面板 `TALK` 指示灯并用
`preflight --id` 区分两种成因，**别反复试本工具**。

⚠ **破坏性**：`RESET` 回到**开机测量配置**（档位/NPLC/功能/触发）→ 必须
confirm=True。返回体含 `idn`（救回来是 `HP3458A`）与一次电压读数。

分流判据见 skill instrument-mcp。"""

PATCHES["ks3458a_configure"] = """3458A 设置直流档位与积分时间（`DCV <range>` / `NPLC <n>`）。**改配置**。

dcv_range 传数值（0.1/1/10/100/1000）→ **固定档**；传 "AUTO" → **自动挡**。
只动自动挡开关请用 ks3458a_autorange(on)（不碰档位/NPLC）。
10 V 档物理可到 12 V（120%），但本库选档按 1.1 倍余量（保守）——确知 ≤12 V 又要用
10 V 档就直接传 10。

**换档/换配置后库内会自动丢弃第一次读数**（建立时间 + 自校准），有意行为。
写后用 ks3458a_status 的 `device` 回读复核；**自动挡状态只能看 `ARANGE?`**。

细节见 skill instrument-mcp →「参数语义速查 · ks3458a_configure」。"""

PATCHES["ks3458a_acv"] = """3458A 交流电压配置（`ACV` / `SETACV` / `ACBAND`，可选 `NPLC`）。**改配置**。

sync=False → `SETACV ANA`（模拟转换，默认）；True → `SETACV SYNC`（同步采样）。
band_lo / band_hi 给定时下发 `ACBAND <lo>,<hi>`（Hz，**必须成对给**）；
nplc 给定时下发 `NPLC <n>`。

⚠ **SYNC 路径未实测**；`ANA` 下 `TARM SGL,1` 已实测能读出交流电压（2026-09-23）。
交流**读数**接口本服务器暂未暴露（库里有 `DMM3458A.read_acv()`）。
详见 skill instrument-mcp。"""

PATCHES["ks3458a_reset"] = """3458A 复位：`RESET` + `END ALWAYS` + `INBUF ON`。

⚠ **破坏性**：回到**开机测量配置**——档位/NPLC/功能/内存/触发等一并复位。
必须 confirm=True。

这是本库**唯一**的重置入口：MCP 连接默认 `reset_on_open=False`，**不会**每次调用
偷偷复位仪表。`END ALWAYS`/`INBUF ON` 是 `TARM SGL` 单次读数配方的前置条件——
缺了会把 GPIB 总线占住。完整影响范围见 skill instrument-mcp。"""

PATCHES["ks3458a_read_series"] = """3458A **一次会话内连续重复测量 n 次**（批量采集主用），n 取 1~1000。

与 read_avg/read_stats 的区别：返回**逐点序列**（可选落 CSV）与时长统计；比连续调
n 次 ks3458a_read 省掉每次 ~0.27 s 重连（100 点 @NPLC 10：~70 s → ~45 s）。
`interval_s` 是**点间间隔**（主机 sleep，非精密时序）；要精密等间隔采样用
ks3458a_burst 的 TIMER。

返回 `{n, values?/summary, csv?}`：values 仅在 n≤50 时内联，更大只回 summary
（防上下文爆炸）——要逐点数据用 save_csv=True。"""

PATCHES["mho_measure_item"] = """MHO 测量查询。ch=1-4；双信源项（延迟/相位）需同时给 ch2。
item 取值：单信源 30 项、双信源 8 项——**完整枚举**见 skill instrument-mcp
→「参数语义速查 · mho_measure_item」。

samples>1 连读 N 次（≤200）返回 mean/min/max/stddev/count/invalid——单次读数抖动
明显，**结论要用均值**；无效读数单独计数。
rails=True 额外读顶轨/底轨（VTOP/VBASe）并**上下分别**判断贴边：顶贴→offset 调更负、
底贴→offset 调更大（方向相反故分开报）。

无有效读数时分类报因（channel_off / off_screen / near_edge / few_edges /
no_signal，附 hint/window/evidence）：**near_edge 可能是部分削顶后的"看着合理的
假值"，必须换档**。探头比 ≠1 时幅度类读数为探头端电压（频率不受影响）。
⚠ 形态判断用 mho_screenshot 看图（设备读数在超屏时不可信）。"""

PATCHES["mho_channel"] = """MHO 通道垂直参数设置（档位/偏置/耦合/探头比/显示）——**写后回读验证**。
**只影响指定通道**（多信号实验台上不动别人的通道）。

三条设备行为已在工具内处理：① **屏幕中心电压 = −offset**（按 +offset 理解会把波形
顶出屏幕）；② 改 scale 会**等比缩放 offset** → 固定按 **scale → offset** 顺序写；
③ 通道 OFF 时写 SCALe/OFFSet 被**静默忽略** → 需要写垂直参数时自动先开通道
（reasons 里说明），display=False 在参数写完后才关。
偏置有硬件量程上限（**随档位变**）：被钳制时 `adjusted` 非空 + reasons 说明。

返回 {requested, before, actual, adjusted, reasons, window=[bottom,top]}。
量程阶梯见 skill instrument-mcp →「参数语义速查 · mho_channel」。"""

PATCHES["mho_fit_channel"] = """MHO **单通道**垂直自动定标/居中——只动该通道的 scale/offset（**不是全局 autoset**）。

判据：① 可测（读数非 9.9E37）② 不贴边（margin 带内）③ 占屏率 ∈ [0.4, 0.9]
（目标 occupancy，默认 0.7）。
平直/无信号 → 判 `flat`：**保持档位**、按均值居中（不猜档位，猜了就是假精度）；
超出量程/未收敛 → ok=false + reason + trace 证据，**不假装成功**。

返回 before/after/measured/occupancy/margin_ok/adjusted/reasons/trace/warnings。
算法与设计依据见 skill instrument-mcp →「参数语义速查 · mho_fit_channel」。"""

PATCHES["mho_get_waveform"] = """MHO 读取通道波形（电压 + 时间轴），返回**摘要** + 可选 CSV 路径。

mode: NORMal=屏幕波形（**1~1000 点**）/ MAXimum / RAW=内存波形（**要求示波器处于
STOP 态**，否则报 device_error 并提示先 `mho_acquisition("stop")`）；
fmt: BYTE(8bit)/WORD(16bit)/ASCii；points 受模式上限约束（超限报 param_validation）。
save_csv=True 存 CSV 到 TEST_DATA/mho/（不返回完整数组，防上下文爆炸）。
**分析频率请用 FFT**（朴素过零对调幅信号会误判）。

换算公式与实测上限见 skill instrument-mcp →「参数语义速查 · mho_get_waveform」。"""

PATCHES["mho_timebase"] = """MHO 水平时基设置（s/div 与水平位移）——**两者都回读**（只回读被写的那个
等于没回读）。

⚠ 时基是**全局**的：影响所有通道的显示与"屏内几个周期"，共享实验台上会改变
别人的观察。**屏内不足 2 个周期时频率类测量读不到**（是窗口不够，不是精度问题）。

返回 {requested, before, actual, adjusted, reasons, window_t}。"""

PATCHES["mho_trigger"] = """MHO 触发设置（边沿源/电平/斜率、模式/扫描）——写后回读，枚举对照手册校验。

slope ∈ POSitive/NEGative/RFALl；sweep ∈ AUTO/NORMal/SINGle；
mode ∈ 手册 3.27.1 的 20 项（EDGE/PULSe/…/IIS/FLEXray/M1553）。
下发顺序 mode → sweep → 源/斜率/电平。
⚠ 触发模式/扫描是**全局**设定，共享实验台上会影响他人观察。
返回 {requested, before, actual, adjusted, reasons}；电平受限时 reasons 里带该
通道窗口范围。"""

PATCHES["sds_get_waveform"] = """SDS 读取通道波形数据（电压 + 时间轴，经 FFT 交叉验证可信）。ch=1-4；
points 默认 50000（受设备 ACQ:POIN? 上限约束；过大很慢）。save_csv=True 时存 CSV
到 TEST_DATA/common/。返回**摘要**（点数/时间窗/Vpp/interval/档位）+ 可选 CSV 路径，
不返回完整数组（防上下文爆炸）。

时间轴可信性依据与实测值见 skill instrument-mcp。
**分析频率请用 FFT/自相关**，朴素过零对调幅信号会误判。"""

PATCHES["sds_measure"] = """SDS 单次测量（SIMPLE 模式，自动切模式 + 设信源）。ch=1-4。
item 取值（SIMPle:ITEM 表，51 项全支持）**完整枚举**见 skill instrument-mcp
→「参数语义速查 · sds_measure」。

无有效读数（如无信号测频率）会在超时后返回 device_error——**属正常现象，非故障**。"""

PATCHES["psu_power_cycle"] = """DH1766 上下电循环：关断→延迟→开启→延迟，重复 cycles 次。

⚠ **需 confirm=True**——授权同 psu_output：① 用户本轮明确要求做上下电/循环，
或 ② 用户明确声明独占使用；否则先向用户确认（可能打断他人实验/板子供电）。

**expect_mode 必填**（仅校验，不符拒绝）。
off_delay_s 默认 1s（需保证下电放电时调大，如实测电容残留需 ≥6s）；on_delay_s
默认 1s；cycles 默认 1。延迟由主机 sleep 控制，**不精准**，仅用于保证放电/上电时序。
返回 {"cycles", "records":[每次循环延迟与电压], "after"}。"""


def iter_doc_nodes(src: str):
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        names = []
        for d in node.decorator_list:
            if isinstance(d, ast.Name):
                names.append(d.id)
            elif isinstance(d, ast.Attribute):
                names.append(d.attr)
            elif isinstance(d, ast.Call):
                f = d.func
                names.append(f.id if isinstance(f, ast.Name) else getattr(f, "attr", "?"))
        if not any(n in ("device_tool", "tool") for n in names):
            continue
        body0 = node.body[0]
        if (isinstance(body0, ast.Expr) and isinstance(body0.value, ast.Constant)
                and isinstance(body0.value.value, str)):
            yield node.name, body0.lineno, body0.end_lineno, body0.value.value


def render(new_text: str, indent: str) -> list[str]:
    lines = new_text.split("\n")
    out = [f'{indent}"""{lines[0]}']
    for l in lines[1:]:
        out.append((indent + l).rstrip())
    out.append(f'{indent}"""')
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not (args.apply or args.dry_run):
        ap.error("specify --dry-run or --apply")

    src = SERVER.read_text(encoding="utf-8")
    lines = src.split("\n")
    nodes = {name: (s, e, old) for name, s, e, old in iter_doc_nodes(src)}

    missing = [n for n in PATCHES if n not in nodes]
    if missing:
        print(f"!! 未在 server.py 中定位到这些工具: {missing}", file=sys.stderr)
        return 2

    plan = [(nodes[n][0], nodes[n][1], n, nodes[n][2], new) for n, new in PATCHES.items()]
    plan.sort(key=lambda x: x[0], reverse=True)

    old_total = sum(len(x[3]) for x in plan)
    new_total = sum(len(x[4]) for x in plan)
    print(f"待改工具 {len(plan)} 个")
    print(f"{'tool':<26}{'old':>7}{'new':>7}{'Δ':>8}{'%':>7}")
    for s, e, name, old, new in sorted(plan, key=lambda x: x[0]):
        print(f"{name:<26}{len(old):>7}{len(new):>7}{len(new)-len(old):>8}"
              f"{(len(new)/len(old)*100):>6.0f}%")
    print(f"{'TOTAL':<26}{old_total:>7}{new_total:>7}{new_total-old_total:>8}"
          f"{(new_total/old_total*100):>6.0f}%")

    if args.dry_run:
        sample = next(x for x in plan if x[2] == "mho_measure_item")
        s, e, name, old, new = sample
        print(f"\n--- 样例 diff: {name} (行 {s}-{e}) ---")
        for line in difflib.unified_diff(
                render(old, "    "), render(new, "    "),
                fromfile=f"{name} (old)", tofile=f"{name} (new)", lineterm="", n=1):
            print(line)
        print("\n[dry-run] 未写入。加 --apply 落盘。")
        return 0

    for s, e, name, old, new in plan:
        lines[s - 1:e] = render(new, "    ")

    new_src = "\n".join(lines)
    ast.parse(new_src)  # 语法自检：失败即抛，不落盘
    SERVER.write_text(new_src, encoding="utf-8")
    print(f"\n[apply] 已写入 {SERVER}")
    print(f"        字符 {old_total} -> {new_total}（省 {old_total-new_total}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
