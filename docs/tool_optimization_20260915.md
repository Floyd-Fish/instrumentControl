# instrument 工具优化清单（2026-09-15 实测）

来源：2026-09-15 用本工具集调试 V2 涡流位移传感器（MHO984D 看 TRIG_OUT/VOUT、DG832 做激励、
SDS 看波形）时逐条踩到的问题。**每条都附实测命令与回读原文**；属于推断而非实测的地方单独标注。

---

## 优先级汇总

| # | 问题 | 影响 | 优先级 |
| - | ---- | ---- | ------ |
| 1 | MCP 服务进程仍跑旧代码，已修的护栏没生效 | 调用方以为"功能不支持"，绕道/误判 | **P0** |
| 2 | 缺"设置类"工具（MHO/DHO 无法设档位/偏置/时基/触发） | 只能裸 SCPI，绕过全部安全封装 | **P1** |
| 3 | 偏置语义无提示 + 改档位会**等比缩放偏置** + 有量程上限 | 读数静默失真（本次实测踩到） | **P1** |
| 4 | 无读数错误信息不区分原因（离屏/边沿不足/无信号） | 误导排查方向，浪费整轮调试 | **P1** |
| 5 | MHO/DHO 缺统计测量（SDS 有） | 单次读数误差大，结论不稳 | P2 |
| 6 | `instr_write` 多条回读不解析（原样拼串） | 字段错位风险 | P2 |
| 7 | 探头比是隐性口径，测量返回里没有 | 幅度读数可能是 10× 误读 | P2 |

---

## 实施状态（2026-09-16 更新）

| # | 状态 | 落地位置 / 证据 |
| - | ---- | --------------- |
| P0-1 | **已完成** | 拒绝文案已与新规则对齐（`instr_query` 现在明说"问号后可带参数"）；新增**启动自检**：服务启动时把查询判据实测结果打一行到 stderr（`server.py::__main__`），根治"改了没生效"的静默。**注意：改代码仍需重启 MCP 才生效**（服务在客户端启动时拉起，不热重载） |
| P1-2 | **已完成** | 新增 `mho_channel` / `mho_timebase` / `mho_trigger`（+ DHO 同三件套）。MCP 工具 50 → 57 |
| P1-3 | **已完成** | `rigol_scope.RigolScope.configure_channel()`：固定 **scale→offset** 顺序、通道 OFF 自动先开、写后**回读比对**；设备没照做时返回 `adjusted` + `reasons`（钳制/吸附/等比缩放分行说明）。`*_status` 每通道附 `center_v = −offset` 与 `window_v` |
| P1-4 | **已完成** | `diagnose_no_reading()`：无有效值分五类（`channel_off` / `off_screen` / `near_edge` / `few_edges` / `no_signal`）+ `hint` + `window` + `evidence`；经 `ToolDiagnosis` 与错误一起返回。**`near_edge` 再分顶/底**（`edges_touching` + `edge_hints`：顶贴→offset 调更负、底贴→offset 调更大，方向相反）；`mho_measure_item(rails=True)` 直接给**顶轨/底轨**（VTOP/VBASe）与各自贴边结论。频域读数成功时也会提示"屏内仅 ~N 个周期" |
| P2-5 | **已完成（主机侧）** | `mho_measure_item(samples=N)` 连读给 `mean/min/max/stddev/count/invalid`（≤200 次）。**设备侧** `:MEASure:STATistic:*` 未做——MHO 手册有该命令族，待需要更高精度时补 |
| P2-6 | **已完成** | `instr_write` 多段回读新增 `readback_fields`（段↔值配对）；段数不符时如实说明，不硬配 |
| P2-7 | **已完成** | 测量返回带 `probe_x`；探头比 ≠1 时附 `warnings` 说明"幅度类读数为探头端电压"；`*_status` 里给 `probe_note` |

**追记（2026-09-22）**：上表 P0-1 的"启动自检"行已改**纯 ASCII 英文**（标记词 `启动自检` →
`startup self-check`），且 `server.py` 在**导入时**把 `sys.stderr` 固定为 UTF-8——原中文行在
中文 Windows 上按 GBK(cp936) 写出，被 MCP 客户端按 UTF-8 解码成替换字符 U+FFFD（DSH 侧
症状＝`xterm.js: Parsing error: ... code: 65533`，traceback 路径行还会被吃半截）。根因、
编码矩阵与回归见 `TEST_RECORDS.md` 2026-09-22 条。

**回归**：`TEST_SCRIPTS/common/verify_rigol_scope_semantics.py`（24 项断言，离线）——把你这次踩到的三条设备行为
（OFF 静默忽略 / 改 scale 等比缩放 offset / 偏置 ±20 V 钳制）做成了**设备模型**（`FakeTrapScope`），
外加"离屏→逐档放大定标""平直不猜档位""超量程如实报"等场景；并复现了 scale 0.9/1.0 的标定证据。

**仍未做**：① SDS 侧同样缺设置类工具（`sds_auto_scale` 只能闭环改，不能定点设）——SDS 的
"中心=−offset/格数"约定**未实测**，按设计文档 §5 的要求先别照抄；② DHO 的格数未标定，故
`dho_fit_channel` 未暴露（`dho_channel/timebase/trigger` 已可用，但未实机验证）。

---

## P0-1 MCP 服务进程仍是旧代码——"已修"的护栏实际未生效

**证据（今天实测，同一分钟）**

```
# 磁盘代码自测（D:\WorkDesigns\3_WorkTools\instrumentControl）
:MEASure:ITEM? VAVG,CHANnel3  ->  seg_query=True  query_only=True  forbidden=False   # 已放行

# 实时 MCP 调用（同一个资源串）
instr_query(':MEASure:ITEM? VAVG,CHANnel3')
-> {"ok": false, "error_type": "forbidden",
    "error": "instr_query 只接受纯查询消息（每个 ';' 分段都以 '?' 结尾）且不得含复位/锁定类命令"}
```

- 磁盘上 `_segment_is_query`（`mcp_instruments/server.py:547`）已按"问号前是合法助记符"判定，
  参数化查询**本应放行**（提交 `288763d`，今天）。
- 但运行中的进程仍按旧规则拒——**MCP 服务在客户端启动时拉起，改代码不会热重载**。
- 顺带：拒绝文案 `server.py:679` 仍是旧规则措辞（"每个 ';' 分段都以 '?' 结尾"），
  而 `dg_query` 的同名文案 `server.py:1436` 已经是新的（"问号后可以带参数"）——
  说明代码已改、只有这一处字符串没跟上，**外加进程没重启**。

**动作**：① 重启 MCP 连接/客户端（本次调试全程被它误导，以为工具不支持参数化查询）；
② 把 `server.py:679` 文案与新规则对齐；
③ 建议加一条自检：服务启动时把 `_segment_is_query(':MEAS:ITEM? VPP,CH1')` 的结果打进日志，
避免"改了没生效"再次静默。

---

## P1-2 缺"设置类"工具——MHO/DHO 只能裸写 SCPI

现状（MHO）：`mho_status / mho_measure_item / mho_screenshot / mho_get_waveform /
mho_acquisition / mho_autoset`；DHO 更少（status + measure_item）。
**没有任何工具能设垂直档位/偏置/耦合/探头、水平时基、触发（源/电平/斜率/模式）。**

本次为了"把 CH3 放到屏幕中间居中"，只能走 `instr_write` 裸 SCPI（绕过库内全部封装）：

```
instr_write(':CHANnel3:SCALe 2;:CHANnel3:OFFSet -3.5', readback_cmd=':CHANnel3:SCALe?')
```

**库里其实早已实现**，只差一层 MCP 薄包装（`rigol_scope/scope.py`）：

| 能力 | 行 |
| ---- | -- |
| `channel_display` / `channel_scale` / `channel_offset` | 202 / 209 / 220 |
| `channel_coupling` / `channel_probe` / `channel_bwlimit` / `channel_impedance` | 228 / 237 / 245 / 253 |
| `timebase_scale` / `timebase_offset` | 266 / 273 |
| `trigger_mode` / `sweep` / `edge_trigger` / `edge_level` | 281 / 289 / 297 / 313 |

**建议**：新增 `mho_channel(ch, scale?, offset?, coupling?, probe?)`、`mho_timebase(scale?, offset?)`、
`mho_trigger(source?, level?, slope?, mode?)`（DHO 同基类，一并暴露）。
SDS 侧同理（目前只有 `sds_auto_scale` 闭环改这些量，不能定点设置）。

> ⚠ 别做成"发完就当成功"：见 P1-3，写 offset 会被设备改掉 / 被量程钳制。

---

## P1-3 偏置语义：中心=−offset、改档位会等比缩放偏置、且有量程上限

这是本次最费时间的一处，三条实测结论如下。

### (a) 约定：屏幕中心电压 = **−offset**

按"中心 = +offset"理解设偏置 → 波形被顶出屏幕，读数变成垃圾值（本次早期读到 0.9216V 就是这么来的）。

### (b) ⚠ 已知行为（本次实测记录）：改 `SCALe` 会**自动改动 offset**——等比缩放，波形保持屏幕位置

**不是保持不变，是设备主动改写**：

| 动作 | `:CHANnel3:OFFSet?` 回读 |
| ---- | ------------------------ |
| 初始 scale 2.0 | −3.5 |
| 写 `SCALe 0.85`（**没动 offset**） | −1.49 ≈ −3.5 × 0.85/2.0（理论 −1.4875，被量化到 10mV） |
| 写 `SCALe 1.0`（**没动 offset**） | −1.75 = −3.5 × 1.0/2.0 |

**记录要点**
1. `offset` 不是独立变量：**它随 `scale` 走**（保持波形在屏幕上的位置不变）。改档位 = 同时改了偏置。
2. 缩放后会被**量化**（−1.4875 → −1.49），所以"按比例反推原值"只能近似。
3. 调用方"设完档位就以为偏置还是原值" → 算出来的屏幕窗口全错（本次就因此把 7.48V 的迹线判到屏幕外）。
4. 因此：**任何设置序列里，scale 必须先设、offset 必须后设**；且**设置后必须把 scale 与 offset 一起回读**——
   只回读被写的那一个等于没回读。
5. 测试脚本侧同样适用：脚本若只记"我设过 offset=−3.5"，中间任何一次改档位都会让这个假设失效。
6. 本条仅在 RIGOL MHO984D 上实测；SDS 是否同约定**未验证**，不要默认套用。

### (b-2) 追加实测（2026-09-15 温度 DAC 测试）：**通道关闭时，档位/偏置写入被忽略**

```
:CHANnel4:DISPlay OFF; :CHANnel4:SCALe 0.05; :CHANnel4:OFFSet 0
-> 回读 :CHANnel4:DISPlay?;SCALe?;OFFSet?  =  0;1.500000E+00;-5.000000E+00   # 没改！
:CHANnel4:DISPlay ON;  :CHANnel4:SCALe 0.05; :CHANnel4:OFFSet 0; :CHANnel4:DISPlay OFF
-> 回读                                   =  0;5.000000E-02;0.000000E+00   # 生效 ✓
```

- **`DISPlay OFF` 状态下写 `SCALe`/`OFFSet` 会被静默忽略**（无错误码，`syst_errors=[]`）。
- 影响：设置类工具若"先关通道再设参数"，会得到一个**看似成功实则没改**的结果——正是 §4.1 那条"写后必须回读"的又一个实例（本次是靠回读发现的）。
- 建议：新增的 `*_channel()` 设置工具里，参数设置与显示开关**解耦**，并在返回里回读全部四项（display/scale/offset/probe）。

### (c) 偏置有硬件量程上限：不是所有档位都能把波形置中

实测（MHO984D / CH3 / 1X 探头）：

```
:CHANnel3:OFFSet -50   （scale 2.0）-> 回读 -2.000000E+01   # 钳到 ±20V
:CHANnel3:SCALe 0.5;:CHANnel3:OFFSet -50 -> 回读 -2.000000E+01   # 与档位无关，仍是 ±20V
```

- 本次在 scale 2.0 / 0.5 上都回读 −20V，据此写下"±20V"——**该结论不完整**。

> **2026-09-16 实机更正（MHO984D / CH3 / 1X，逐个档位试写 ±50 V 看回读）**：
> **偏置量程随档位变**，是一条阶梯：
>
> | scale (V/div) | 0.05 | 0.1 | 0.2 | 0.5 | 1 | 2 | 5 | 10 |
> （上限用"写 −1000 V 看回读"探，写 ±50 回读 ±50 只说明 ≥50）
> |---|---|---|---|---|---|---|---|---|
> | 偏置上限 | **±1 V** | ±10 V | ±10 V | ±20 V | ±20 V | ±20 V | **±100 V** | **±100 V** |
>
> 影响：**小档位下"把波形居中"可能根本做不到**（0.05 V/div 只允许 ±1 V 偏置），
> 必须**先抬档位**再置中。`fit_channel()` 已改为**闭环自适应**：目标偏置被钳制就逐档抬高
> 重试，不写死这张表（以设备回读为准）。DHO/SDS 未实测，不套用。

**工具侧建议**
1. `*_status` 除 offset 数值外，直接给出**屏幕中心电压 = −offset**，并给出当前窗口范围；
2. 设置类工具**写后回读全部相关量**；`requested != actual` 时返回 `adjusted=true` + 原因
   （"被设备钳制到 X" / "因档位变化被等比缩放"），**不要 `ok:true` 混过去**；
3. 提供显式 `center_at(voltage)` helper：内部算 `offset = −V`、回读确认，超量程时明确报
   "该档位无法置中（可设范围 ±X）"，并建议合适的 scale。

### (d) 机型常量（实测标定，供离屏判据使用）

MHO984D **垂直 8 格**、中心 = −offset，故窗口为：

```
V_window = [ −offset − 4·scale , −offset + 4·scale ]     # 1X 探头
```

标定方法与证据（可当回归用例）：

| 设置 | 窗口上沿 | 7.4816V 信号的 `:MEASure:ITEM? VAVG,CHANnel3` |
| ---- | -------- | ---------------------------------------------- |
| scale 0.9, offset −3.5 | 7.1 V | `9.9000E+37`（无有效值）|
| scale 1.0, offset −3.5 | 7.5 V | **7.4825**（有效）|

18mV 的边界余量下两种结果都对上 → **8 格 + "中心=−offset" 定量成立**。
水平格数未单独标定（由"20µs/div 看 5kHz 测不到频率"反推约 10 格），
建议工具按机型常量 + 一次实测标定，不要照抄本行的推断值。

---

## P1-4 无读数错误信息不区分原因，且不提"离屏"、不给截图提示

**现状**（`rigol_scope/scope.py:357-360` → `param_validation`）：

```
测量项 VAVG@CHANnel3 无有效值（设备返回 9.9000E+37）——检查信号接入/触发/档位
```

这一句把三种**完全不同**的原因混在一起：

| 真实原因 | 本次实例 | 现有提示能否定位 |
| -------- | -------- | ---------------- |
| 迹线离屏（偏置/档位不当） | CH3 紫线完全不可见（截图存证：`E_distance/test_scripts/results/mho_ch3_offscreen_evidence_20260915_143645.png`，原始产物 `TEST_DATA/mho/mcp_mho_20260915_143645.png`） | ✗ 只字未提 |
| 时基窗口内边沿不足 | 20µs/div 看 5kHz（窗口 1 个周期）→ CH1 频率读不到 | ✗ 只字未提 |
| 通道没接信号 | — | ✓ 勉强沾边 |

**工具能自己算出来，不必让人截图**（P1-3(d) 已给出窗口公式）：

1. **离屏判据**：由 `scale`+`offset`+垂直格数算窗口，迹线（或读数）不在窗口内/触界 →
   `suspicious="off_screen"`，并给建议（"抬高 scale 到 ≥ X 或改 offset"）；
2. **边沿判据**：窗口时长 = 水平格数 × 时基；若 < 2×被测周期 → `suspicious="few_edges"`，
   直接给"时基放宽到 ≥ 100µs/div"这类**具体数值建议**
   （本次实测：20µs/div 测不到，改 100µs/div 立刻读到 **4.9993 kHz**——不是精度问题）；
3. 返回体统一加 `suspicious` / `hint` 字段；
4. **判据命中时工具主动截一张图并附路径**（`*_screenshot` 已能返回可直接 Read 的 PNG）。
   截图作为**兜底**很好用，但不该是唯一手段——本次就是"档位是我自己设错的，
   所以没意识到要怀疑它"，docstring 里写了"请看截图"并没能拦住我。

---

## P2-5 MHO/DHO 缺统计测量（SDS 有）

用户原话："示波器测试过程中你可以使用统计平均值，不然你这个误差范围很大。"
本次 VOUT 定标就是靠读屏上的"平均值(C3)=7.4821V"才稳住的（单次读数抖动明显）。

- SDS 有 `sds_meas_statistics`（on/max_count/histogram/reset + `MEAN/MIN/MAX/STD/CNT`），**MHO/DHO 没有**；
- MHO900 编程手册有对应命令族 `:MEASure:STATistic:COUNt / DISPlay / RESet / ITEM?`
  （`<type> ∈ MAXimum|MINimum|CURRent|AVERages|DEViation|CNT`），**库与工具都没有实现**。

**建议**：新增 `mho_meas_statistics`（与 SDS 对齐）；或给 `mho_measure_item` 加 `samples=N`
（主机侧连读 N 次返回 `mean/min/max/stddev`，不改设备也能用）。

---

## P2-6 `instr_write` 的多条回读不解析

`readback_cmd` 名义上是"单条"，但 `;` 串联的纯查询会被整条发出、**响应原样拼串返回**：

```
instr_write(':CHANnel3:SCALe 2;:CHANnel3:OFFSet -3.5',
            readback_cmd=':CHANnel3:SCALe?;:CHANnel3:OFFSet?')
-> "readback": {"cmd": "...", "response": "2.000000E+00;-3.500000E+00"}
```

没有字段名、没有顺序说明，调用方要自己数字段——写错顺序不会报错。
**建议**：允许传数组（逐条发/逐条回、带字段名）；或在文档里明确"只支持单条"。

---

## P2-7 探头比是隐性口径

`mho_status` 本次回读：`ch1 probe_x=10.0`、`ch2 probe_x=10.0`、`ch3 probe_x=1.0`、`ch4 probe_x=1.0`
（一次测试里混用两种口径，屏幕上腿标也是 10X/1X）。

- **频率测量不受探头比影响**，但**幅度/触发电平类读数会**；
- 本次 CH1 测 TRIG_OUT（3.3V 逻辑）得 `VPP=4.2843V`——需要"探头比 + 实际接线"才能判断真假
  （10X 探头 + 长地线环的过冲是合理解释，也可能是设置错误；**未定性**）。

**建议**：测量返回里带 `probe_x`（或"输入口径"字段）；`*_status` 里把探头比放醒目位置。

---

## 不要改坏的良好设计（本次受益）

- `instr_query` 拦写命令：`:IDN?;*RST` 自测 `forbidden=True` ✓；
- `instr_write` 的审计 jsonl（仓库锚定 `TEST_DATA/common/mcp_scpi_audit_YYYYMMDD.jsonl`，不随 CWD 漂移）
  与超时 `executed="unknown"` 语义；
- `dg_protect` 强制保护 + 输出联锁、`sdg_output` 的 `expect_load`、`psu_*` 的 `expect_mode`；
- `*_screenshot` 返回可直接 Read 的 PNG 路径——本次靠它看穿"CH3 离屏"。

---

## 附录：可直接当回归用例的实测记录

| 用例 | 命令 | 期望 |
| ---- | ---- | ---- |
| 参数化查询放行 | `instr_query ':MEASure:ITEM? VAVG,CHANnel3'` | 返回读数（当前被拒 → P0-1） |
| 写命令仍被拦 | `instr_query '*IDN?;*RST'` | `forbidden` |
| 改档位缩放偏置 | 写 `:CHANnel3:SCALe 1`（此前 offset=−3.5@scale2） | `:CHANnel3:OFFSet?` → −1.75，工具应报告"偏置已被设备等比缩放" |
| 偏置量程钳制 | `:CHANnel3:OFFSet -50`（1X） | 回读 −20V，工具应报 `adjusted=true` |
| 离屏诊断 | scale 0.9 / offset −3.5，`VAVG@CH3`（信号 7.4816V）| 无有效值，工具应给 `suspicious="off_screen"` + 建议 scale ≥ 1.0 |
| 边沿不足诊断 | 20µs/div 下测 5kHz 频率 | 无有效值，工具应给"时基放宽到 ≥ 100µs/div" |
| 统计平均 | `mho_meas_statistics` 或 `samples=N` | 返回 `mean/min/max`（当前无此能力） |

设备基线（本次测试后已恢复）：MHO984D CH3 `SCALe 2` / `OFFSet −3.5` / 1X，VOUT 读数 **7.4816V**。
