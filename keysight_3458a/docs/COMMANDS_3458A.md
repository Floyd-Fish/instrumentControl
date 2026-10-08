# 3458A 命令表（白名单 + 出处 + 实现状态）

> ## 2026-09-23 真机实测补充（本机 GPIB0::9，82357B + Keysight VISA）
>
> **接通方法（唯一可行组合，已落码）**：`ktvisa32.dll`（Keysight VISA 核心）+
> 先 `SetDllDirectoryW(<Keysight IO Libraries Suite>\bin)` 并预加载 `ioGPIB.dll` /
> `ioGpibIntfc.dll`，再 `viOpen("GPIB0::9::INSTR")`。
> 系统默认 `C:\Windows\System32\visa32.dll`（NI/IVI 壳）打开 GPIB 报
> `VI_ERROR_LIBRARY_NFOUND`——它的 GPIB 护照要 NI-488.2 或 32 位 Tulip 护照，本机都没有；
> SICL 的 `iopen` 在同一环境下抛 `0xE06D7363`。实现见
> `keysight_3458a/transport.py::KeysightVisaTransport` 与 `prepare_keysight_visa()`。
>
> **实测响应样例（这些查询都可用 → 推翻下面第 11 条"无回读命令"的旧判断）**：
> `ID?`=`HP3458A`；`ERRSTR?`=`0,"NO ERROR"`；`TEMP?`=`37.0`（数值，单位按 °C 采信）；
> `TARM?`=`4`(HOLD)/`1`(AUTO)；`TRIG?`=`1`(AUTO)/`4`(HOLD)；`NRDGS?`=`1, 1`；
> `NPLC?`=`10.0000000E+00`；`APER?`=`200.000000E-03`；`FUNC?`=`1, .1`（功能码+档位）；
> `RANGE?`=`.1`；`AZERO?`=`1`；`MEM?`=`0`；`OFORMAT?`=`1`；`MFORMAT?`=`4`；
> `INBUF?`=`1`；`END?`=`2`；`ISCALE?`=`1.00000000E+00`。
>
> **读数实测**：`TARM SGL,1` → `-2.43E-06`（0.43 s/次 @NPLC=10, 50 Hz）；
> 连续 3 次 mean≈`-2.16E-06`、sd≈`4.5E-08`（输入近零，100 mV 档）。
>
> **坑（实测）**：若 `TRIG?`=4(HOLD)（上次会话留下的挂起态），`TARM SGL,1` **永远不出数**
> （20 s 超时），必须先 `TRIG AUTO`。参考实现的 `reset()` 里 `RESET` 恰好把它复位成 AUTO，
> 所以此前未暴露。本库用 `prepare_for_read()` 显式补上，**不发 RESET**。
> 现场还实测到一次"上次会话把表留在 free-run"（持续吐读数）——已按 `recover()` 处理。
>
> **⚠ RESET 不复位 TARM（2026-10-08 真机实测，修正上文的推断）**：复位前后
> `TARM?` 均 = `4(HOLD)`——RESET 并**没有**把触发臂回上电值 `TARM AUTO`。手册
> （UserGuide p.226-227）声称 RESET 回到 power-on state，但其列出的具体动作不含
> TARM/TRIG（txt 提取版在"executes these commands"处清单被截断，无法从手册正面
> 确认；上电值表 Table 5 p.25-26 是 `TARM AUTO`/`TRIG AUTO`）。**面板恢复刷新
> 必须显式 `TARM AUTO`**——`release_to_front_panel()` / `ks3458a_release` /
> `ks3458a_restore` 已内置。上文"参考实现的 reset() 恰好复位成 AUTO"针对的是
> **TRIG**（保留，未单独复测）；**TARM 的对应推断不成立，以本条实测为准**。

本文件是 `keysight_3458a` 的**命令白名单登记簿**。`commands.py` 只允许出现下表里的命令；
新增命令必须先在**本文件**登记并写明出处（AGENTS.md 铁律 1「命令禁止猜测」）。

## 出处代号

| 代号 | 出处 |
|---|---|
| `[SICL]` | `D:\MyProjects\EmoeR_D\研发中项目\EmoeCalibrator\Software\cal_tool\dmm_sicl.py`（实战版 SICL 驱动，行号见下表） |
| `[VISA]` | 同目录 `cal_devices.py` 的 `DMM3458A_VISA`（pyvisa 版） |
| `[SAMPLE]` | `…\EmoeCalibrator\3458\python3458A-100k\python3458A_100k.py`（Keysight 官方样例：100k rdg/s） |
| `[SAMPLE2]` | 同目录 `python3458A_10V_100NPLC.py` |
| `[TOOLS]` | 同项目 `Software\cal_tool\tools\tc_attrib_temp.py`、`tc_temp_sweep.py`——在 **3458A 会话**上查内部温度（`dmm._gpib.query("TEMP?")`） |
| `[AC]` | 同项目 `Software\cal_tool\ac_1khz_probe.py`、`ac_stability.py`、`ac_verify.py`——3458A 交流测量配方（`SETACV` / `ACBAND` / `ACV`） |
| `[TASK]` | 本项目任务书给出的命令——**参考实现与官方样例中均未出现**，一律标"未验证" |

> **重要**：`[SICL]`/`[VISA]`/`[SAMPLE]`/`[SAMPLE2]`/`[TOOLS]`/`[AC]` 六条都在真机上跑过
> （EmoeCalibrator 项目），是本库的"有出处"依据；`[TASK]` 只表示"任务书要求实现"，
> **不代表真机验证过**。

## 命令表

| 命令 | 参数 | 语义 | 出处 | 实现位置 | 状态 |
|---|---|---|---|---|---|
| `ID?` | — | 身份（3458A **没有** `*IDN?`；返回形如 `HP3458A`） | `[SICL]` L392-394、`[VISA]` L639-640 | `DMM3458A.idn()` | 已实现（真机跑过） |
| `ERRSTR?` | — | 读错误队列（**没有** `SYST:ERR?`）；形态 `<code>,"<message>"` | `[SAMPLE]` L39 | `DMM3458A.error_string()` / `is_error_clear()` | 已实现；**队列语义未核对** |
| `TEMP?` | — | 3458A **内部温度**读数 | `[TOOLS]` `tc_attrib_temp.py:96`、`tc_temp_sweep.py:135` | `DMM3458A.temperature()` | 已实现（参考项目真机跑过；**单位/精度待手册核对**） |
| `RESET` | — | 复位到开机测量配置（**不是** `*RST`）。⚠ 2026-10-08 实测：**不复位 TARM**（复位前后 TARM?=4 HOLD）；面板恢复需显式 `TARM AUTO`（见文件头注记与手册 p.226-227 核对） | `[SICL]` L320、`[VISA]` L569、`[SAMPLE]` L35 | `DMM3458A.reset()` | 已实现；影响范围：读数中止/错误与状态寄存器/读数内存已核对（p.226-227），TARM 实测不复位；其余按手册 |
| `END ALWAYS` | — | 每次读数都置 EOI | `[SICL]` L325、`[VISA]` L571 | `reset()` 内 | 已实现 |
| `INBUF ON` | — | 打开输入缓冲（`TARM SGL` 配方的**前置条件**，否则占住 GPIB 总线） | `[SICL]` L326、`[VISA]` L575 | `reset()` 内 | 已实现 |
| `TARM HOLD` | — | 停止后续触发（free-run 的第一道闸） | `[SICL]` L318 | `recover()` / `reset()` / `close()` | 已实现 |
| `TARM SGL,1` | — | 触发**一次**读数并回值（命令不带问号） | `[SICL]` L370、`[VISA]` L615 | `read_dcv()` / `read_acv()` | 已实现（DCV 真机跑过） |
| `TARM SYN` | — | 同步触发（进入等待，由随后的读数取走） | `[SAMPLE]` L64 | `read_burst()` | 已实现（官方样例配方） |
| `TRIG HOLD` | — | 触发源保持（不自动重触发） | `[SICL]` L319、`[SAMPLE2]` L48 | `recover()` / `reset()` | 已实现 |
| `TRIG AUTO` | — | 自动触发（数字突发配方里用） | `[SAMPLE]` L56 | `read_burst()` | 已实现 |
| `DCV <range>` | 档位 V | 直流电压档位 | `[SICL]` L333、`[VISA]` L581 | `configure_dcv()` / `set_range()` / `read_burst()` | 已实现 |
| `NPLC <n>` | PLC 倍数 | 积分时间（越大越准越慢） | `[SICL]` L334、`[VISA]` L582 | `configure_dcv()` / `set_nplc()` | 已实现；**AC 下语义未核对** |
| `ACV <range>` | 档位 V | 交流电压档位 | `[AC]` `ac_stability.py:59` 一带 | `configure_acv()` | 已实现（参考项目真机跑过；**AC 配方细节待核对**） |
| `SETACV ANA` | — | 交流转换方式：模拟（>10 Hz 用） | `[AC]` `ac_1khz_probe.py:84`、`ac_verify.py:130` | `configure_acv()` | 已实现 |
| `SETACV SYNC` | — | 交流转换方式：同步采样（<10 Hz 用） | `[AC]` `ac_stability.py:55`、`ac_verify.py:125` | `configure_acv()` | 已实现 |
| `ACBAND <lo>,<hi>` | Hz | 交流带宽（显著影响起伏/噪声） | `[AC]` `ac_1khz_probe.py:85`、`ac_verify.py:133` | `configure_acv()` | 已实现 |
| `PRESET DIG` | — | 数字表预设（整组采样参数复位到数字档） | `[SAMPLE]` L43 | `read_burst()` | 已实现（官方样例配方） |
| `MFORMAT SINT` | — | 内存格式 = 2 字节有符号整数 | `[SAMPLE]` L45 | `read_burst()` | 已实现 |
| `OFORMAT SINT` | — | 输出格式 = 2 字节有符号整数 | `[SAMPLE]` L46 | `read_burst()` | 已实现 |
| `MEM OFF` | — | 关读数内存（数据直接走总线） | `[SAMPLE]` L49 | `read_burst()` | 已实现 |
| `TIMER <s>` | 秒 | 采样间隔 | `[SAMPLE]` L48 | `read_burst(sample_interval_s=…)` | 已实现 |
| `APER <s>` | 秒 | 孔径时间 | `[SAMPLE]` L47、`[SAMPLE2]` L52 | `read_burst(aperture_s=…)` | 已实现 |
| `NRDGS <n>` | 个数 | 一次触发的读数个数 | `[SAMPLE]` L55 | `read_burst(n)` | 已实现 |
| `ISCALE?` | — | 查询 SINT 读数的换算因子（V/LSB），真值 = 整数 × ISCALE | `[SAMPLE]` L59 | `read_burst()` | 已实现 |

**档位全集**：`0.1 / 1 / 10 / 100 / 1000` V；10V 档有 20% 超量程（可用到 ±12V，`[VISA]` L594）。

## 明确**不在**白名单（禁止添加）

`OHM` / `DCI` / `ACI` / `FREQ` / `AZERO` / `FUNC?` / `RANGE?` / `DCV?` / `NPLC?` /
`MATH` / `SRQ` / `TARM?` / `MEM` 系列 / `NDIG` / `DISP` 等——任务未授权，也没有参考实现
或官方样例佐证。需要时先与用户确认并补手册出处。

## 二进制突发（`read_burst`）配方与解析

逐条对应 `[SAMPLE]` L43-69：

```
PRESET DIG → [DCV <range>] → MFORMAT SINT → OFORMAT SINT
→ [APER <s>] → [TIMER <s>] → MEM OFF → NRDGS n → TRIG AUTO
→ ISCALE? → TARM SYN → 读 2n+2 字节
```

- 解析：**前 2n 字节**按 2 字节**大端有符号**整数切出 n 个，各自 × `ISCALE` 得电压；
- 样例固定多读 2 字节（`read_bytes(nrdgs*2+2)`）——**这 2 字节的含义未核对**（见下）；
- 字节数不足 **明确报错**，不返回截断数据（截断会解析出错位的"合理值"，比报错危险）。

## 手册核对结果（2026-09-23，逐条引用页码）

手册：`E:\手册与技术支持\设备资料与文档\3458A\Ag_3458A_UserGuide_en.pdf`
（Agilent 3458A User's Guide）。**完整核对记录见
`docs/3458a_manual_verification_20260923.md`**（含 Table 5 上电状态全表与实测对照）。
下面只列结论：

### 已关闭（原「待手册核对」项）

| # | 原问题 | 结论（页码） |
|---|---|---|
| 1 | `TEMP?` 单位/格式 | 内部温度，单位**摄氏度**（p.37/p.50）；实测返回纯数值 `37.0`/`36.9` |
| 2 | `SETACV` 取值全集 | type = **analog / random / synchronous** …（p.233）；上电 = `ANA`（p.26）；本库实现 ANA/SYNC 两个子集 |
| 3 | `ACBAND` 范围/默认/单位 | 指定**输入信号频率含量**（p.66）；上电默认 `20, 2E6`＝20 Hz–2 MHz（p.26）；参数单位为 Hz |
| 4 | `ERRSTR?` 队列语义 | 读**并清除**错误寄存器/辅助寄存器中**最低置位**的一位（100 系/200 系），返回 `code,"msg"`，需**反复查询**逐位清；≤255 字符（p.178） |
| 5 | `RESET` 影响范围 | 回到 **Table 5「Power-On State」**（p.26 全表、p.32 说明"returns you to the power-on state"）：档位/功能/触发/NPLC/内存/END(→OFF)/INBUF(→OFF) 等一并复位 |
| 7 | `ISCALE?` 语义 | 返回 **SINT/DINT 格式读数**的换算因子（p.187） |
| 8 | `NRDGS` 有效范围 | **1 – 16777215**（p.207）；`BURST_MAX_READINGS=1e6` 是本仓自设上限（非设备限制） |
| 11 | 是否存在回读命令 | **有**：p.153「Additional Query Commands」明确"任何可配置命令 + `?`"均可查询；实测 `FUNC?/RANGE?/NPLC?/TARM?/TRIG?/…` 全部可用 |
| 13 | "抑制 CR LF"的等价做法 | 手册说明 ASCII 读数以 `cr,lf` 结尾（p.176/p.210），且 `INBUF OFF` 会占住总线直到命令执行完（p.187）→ 本库 `INBUF ON` + LF 终止符合规 |

### 顺带核对到的关键码值（手册原值）

`TARM` 1=AUTO/2=EXT/3=SGL/4=HOLD（p.251，**SGL 触发一次后自动回 HOLD**）·
`TRIG` 1=AUTO/2=EXT/3=SGL/4=HOLD（p.257，上电 AUTO、默认 SGL）·
`END` 0/1=ON/2=ALWAYS（p.176，上电 OFF、默认 ALWAYS）·
`INBUF` OFF/ON=1（p.186，上电 OFF、默认 ON）·
`OFORMAT`/`MFORMAT` 1=ASCII/**2=SINT**/3=DINT/**4=SREAL**（p.210/p.199，MFORMAT 上电=SREAL；执行 MFORMAT 会**清空读数内存**）·
`AZERO` ON/OFF/ONCE（p.162，上电 ON）·
`NPLC` 0–1（细步进）/1–10/10–1000 步进 10（p.205，上电 10）·
10 V 档超量程按 **120% of range**（=12 V）表述，>120% 应改用 DINT（p.136/p.173）·
`ARANGE` ON=1 / OFF=0 / ONCE（p.160-161；**自动挡状态只能看 `ARANGE?`**——实测 `FUNC?`
在自动挡下仍返回固定档数值）·
`APER` 单位**秒**、有效 0–1 s（步进 100 ns，<500 ns 取最小 500 ns），且"overrides any
previously specified integration time or resolution"（p.160）→ **APER/NPLC 不是互斥而是
同一积分时间的不同写法，后设者生效**（`APER?` 返回当前积分时间，实测 NPLC 10@50 Hz → 0.2 s）。

### 6.x 后续实测补齐（详见 `docs/3458a_manual_verification_20260923.md` §6）

- **DINT 突发**已实现并实测（4 字节/读数；手册 p.173：DINT 满量程 = 档位 ×500%）；
- **突发收尾**：`TARM/TRIG HOLD` + clear + 有界 drain，随后 **`PRESET NORM`**（p.217，
  非 RESET）恢复 ASCII 输出——否则输出留在 SINT，后续 ASCII 读数必超时；
- **AC 单次读数**已实测：`TARM SGL,1` 在 `SETACV ANA` 下返回交流读数（4.11 mV AC）；
- **性能**：`connect(recover="auto")` 快速路径（0.27 s）+ 失败再恢复，替代原先每次
  5.11 s 的强制恢复（原因：VISA 超时最小粒度 ≈2 s，一轮 drain ≈2 s）。

### 仍未覆盖（保持"不猜"）

| # | 待核对项 | 现状 | 影响 |
|---|---|---|---|
| 6 | 突发尾部 2 字节的含义（Keysight 样例为何读 `2n+2`） | 手册**未描述**（属样例实现细节）；本库 SINT 只取前 `2n` 字节、DINT 精确取 `4n`，字节数不足即报错 | 末尾读数是否被丢弃（不猜） |
| 9 | AC 功能下 `NPLC` 的语义（`SETACV SYNC` 积分含义是否同 DCV） | 手册未给等效换算；`set_nplc()` 已标注 | AC 测量正确用法 |
| 10 | AC 下单次读数的配方是否同为 `TARM SGL,1` | ✅ **已实测**（2026-09-23）：`SETACV ANA` 下返回 `4.11E-03 V AC`；`SETACV SYNC/RNDM` 采样法未测 | 库 `read_acv()` 可用 |
| 12 | `APER` 与 `NPLC` 的互斥/优先关系 | ✅ **手册已答**（p.160）：同一积分时间的两种写法，APER"overrides any previously specified"，后设者生效（实测 NPLC10@50Hz → APER?=0.2 s） | — |
| — | `SINT` 溢出边界（>120% 档位） | ✅ **已支持 DINT**：`read_burst(data_format="DINT")` 实测通过（手册 p.173：DINT 满量程=档位×500%） | 大信号突发 |
