---
name: instrument-mcp
description: instrument MCP 服务器使用指引 — 八台仪器（SDS 示波器/SDG 信号源/Keysight 34465A 万用表/DHO 示波器/MHO900 示波器/DG832 信号源/DH1766 电源/Keysight 3458A 八位半万用表）的 MCP 工具选择、参数语义、安全门、典型工作流，compact/legacy 双 profile 用法，以及 3458A 非 SCPI 用法与 GPIB/VISA 分层排查（preflight）。触发条件：使用 instrument MCP 工具、compact profile 的 instr_devices/instr_search/instr_describe/instr_call/instr_batch、或 legacy profile 的 sds_/sdg_/dmm_/dho_/mho_/dg_/psu_/ks3458a_/instr_ 前缀工具、仪器测量/定标/截图/关机决策、3458A、GPIB、VISA、仪器连不上/不通/读超时、仪器排查、preflight。
---

# instrument MCP 使用指引

MCP server：`mcp_instruments/server.py`（68 操作 = 64 设备专用 + 3 通用护栏 + 1 故障兜底，八台设备）。
**暴露方式取决于 profile**（`--profile=` 或环境变量 `INSTRUMENT_MCP_PROFILE`，默认 `compact`）：

| profile | 模型看到的工具 | 本文的用法 |
|---|---|---|
| `compact` | 只有 5 个：`instr_devices` / `instr_search` / `instr_describe` / `instr_call` / `instr_batch` | **默认档**；下文的名字是**操作**，需经 `instr_call` 调用，见下 |
| `legacy` | 68 个操作各一个工具（`sds_measure`、`dg_set_wave` …） | 下文决策树里的名字**直接可用** |

## 零、compact 是默认档（先读这一节）

本文下文用 legacy 工具名书写，因为那是最短、最稳定的称呼。在 compact 下**不要直接调它们**，
按下述方式换用即可——**能力完全一样，路径同一条**：

```
instr_devices()                      有哪些仪器（默认**瞬时**：只读配置+上次成功缓存）
instr_devices(full=true)             全量发现（扫各网段；本机实测 355.9s，且会占住执行器）
instr_search("设置的词")              找操作，返回规范 id + 一句话摘要
instr_search("")                      **列出全部能力目录**（按设备族分组）——不知道用什么词时先用它
instr_describe("sds_measure")        取该操作的完整参数表 / 说明 / 安全属性 / 关键约束
instr_call(op, args)                 执行一次（op 用规范 id 或 legacy 名都行）
instr_batch(plan)                    组合执行（扫频 / 批采 / 参数矩阵）
```

**`instr_devices` 默认不做扫描**：它读 `devices.json` 配置 + `last_good_resources.json`
缓存，瞬时返回（实测 0.03s）。只有**新接仪器 / 换了 IP 网段 / 已知地址全连不上**时才
值得用 `full=true` —— 那会扫本机所有网段（本机两个 /16 共 13 万台主机，355.9s），
期间**其它仪器调用一律返回 device_busy**，还可能超出客户端默认 60s 的工具超时。

**规范 id = legacy 工具名把首个下划线换成点**：

```
sds_measure      →  sds.measure
dg_set_wave      →  dg.set_wave
instr_query      →  instr.query
ks3458a_status   →  ks3458a.status
```

所以本文里任何 `sds_xxx` / `dg_xxx` / `ks3458a_xxx` 都可以照此翻译后用 `instr_call` 调用，
例如本文写 `sds_measure(item="PKPK", ch=1)`，compact 下就是：

```
instr_call(op="sds.measure", args={"item": "PKPK", "ch": 1})
```

两点必须知道：

- **参数名与默认值完全一致**，照 `instr_describe` 给的参数表填；必填缺失或类型不符会**在本地被拒**，
  不会下发到设备。
- **安全门一样生效**（`confirm=True`、黑名单、DG832 保护联锁都在操作实现与库层，不经前端放行）。
  换句话说：在 compact 下调用有副作用的操作，**同样**要按本文的 `confirm` / 前置保护要求来。

组合任务（扫频、批量采集）优先用 `instr_batch` 而不是反复 `instr_call`：前者**整批一个执行单元**，
期间别的调用不会插进来（扫频需要这个），也不会为每一步付一次模型往返。

---

本文是 AI 选择工具/参数时的决策依据。

## 零.五、场景路由：走 MCP 工具，还是写脚本直连库？

本项目**两种入口并存**，能力同源（最终都进 `*_control` 设备库 + broker 护栏）。
**按场景选，不按偏好选**：

| 场景 | 用什么 | 理由 |
|---|---|---|
| 临时看一眼（读个测量值/查个状态/截个图）| **MCP 工具**（compact 的 `instr_call` 或 legacy 具名工具）| 一次往返，无需起进程/写文件 |
| 复杂流程（扫频 / 批采 / 参数矩阵 / 循环条件判断）| **写脚本**（`TEST_SCRIPTS/` 里，import 设备库直连）| 模型侧循环=多倍往返且易断；脚本是原子执行+自动留痕 |
| 正式测试 / 回归 / 要留证据链的实验 | **写脚本**（套用 `verify_*.py` 范式）| 产物落 `TEST_DATA/`、带时间戳、可重跑 |
| 一次性组合操作但不想写文件 | MCP 的 `instr_batch`（声明式 plan）| 整批一个执行单元，步间不被插入 |
| 设备排障 / 需要看原始 SCPI 响应 | 脚本 + `common.visa_client`（或 MCP `instr_query`）| 需要逐条控制命令与响应形态 |

**脚本直连的标准头**（护栏不降级的关键——黑名单/查询判据与 MCP 同一份代码）：

```python
import sys
from pathlib import Path
# 仓库根：按脚本所在深度相对定位，**勿写死本机绝对路径**（换机器/换 clone 目录即失效）
ROOT = Path(__file__).resolve().parents[2]     # TEST_SCRIPTS/<设备>/xxx.py 的深度
sys.path.insert(0, str(ROOT))
from instrument_runtime import broker          # decide_scpi / drain_errors / audit_scpi
from sds_control import find_sds               # 各设备库，地址走解析层（不写死 IP）

dev = find_sds()
try:
    d = broker.decide_scpi("C1:VDIV?")          # 纯查询 → 放行；写/复位/锁定 → 拒
    print(dev.query(d.normalized))
finally:
    dev.close()
```

要点：
- **写 SCPI 前必须过 `broker.decide_scpi()`**——它与 MCP 工具走同一黑名单（复位/远程锁
  一律拒，纯查询放行）。绕过它 = 绕过安全门。
- **地址一律走解析层**（`resolve()`/`find_*()`），禁止在脚本里写死 IP（换网段即失效，
  还可能把命令发给别的设备）。
- **并发纪律同样适用**：脚本与 MCP 单例是两个进程，可同时摸同一台仪器——跨进程不互斥，
  会话锁只给 `warnings` 告警。开跑前确认没有别的客户端在用（见「串行化的边界」）。
- **内存**：`import common` 会自动设 `OPENBLAS_NUM_THREADS=1`（2026-10-08 起）——
  脚本进程也只需 ~50 MB，跑完即释放，无常驻成本。

MCP 侧（单例 `127.0.0.1:8765/mcp`）与脚本侧**可以并行使用不同设备**；同一台设备则遵守
上面的并发纪律。

## 一、工具选择决策树

```
需要知道有哪些设备在线？
  → instr_discover（LAN 网段 + USB/GPIB/串口全探测；串口被占用给提示，
    驱动挂起 6s 硬超时；代理 fake-IP 干扰 LAN 时降级 warning 不影响 VISA 结果；
    串口换号/新设备进场后用它重新定位）

新设备 / 无专用库的设备 / 库里没有的能力？
  → instr_query + instr_write（对照手册直发 SCPI，零代码接入，见下方"通用护栏"）

示波器（SDS）：
  看波形显示是否正常 → sds_diagnose（触发链路）→ 异常则 sds_auto_scale
  读测量值 → sds_measure（item 见下）
  判断削顶/居中/有无波形 → sds_screenshot（返回 PNG 路径，**直接 Read 即可看图**；
  设备测量值超屏被钳制不可信，截图是物理真相）
  测量判据/统计/门限 → sds_meas_threshold / sds_meas_statistics / sds_meas_gate
  延迟测量/显示策略 → sds_meas_dtime / sds_meas_display
  全量状态 → sds_status

信号源（SDG）：
  设波形 → sdg_set_wave（不动输出开关）
  开/关输出 → sdg_output（**开/关都需 confirm=True**，expect_load 必填）
  看当前配置 → sdg_status

万用表（DMM，Keysight 34465A，标准 SCPI）：
  测量 → dmm_measure（先 dmm_configure 设功能/量程更稳）
  看配置 → dmm_status

八位半万用表（3458A，**非 SCPI**，`ks3458a_*`）：
  看状态 → ks3458a_status（`ID?`/`ERRSTR?`/`TEMP?` + **`device` 设备回读**
    `FUNC?`/`RANGE?`/`NPLC?`/`TARM?`/`TRIG?`/`INBUF?`… → 真实配置，不是"本会话设过什么"）
  取数 → ks3458a_read（单次 DCV）/ ks3458a_read_avg(n) / ks3458a_read_stats(n)
        **多次重复测量 → ks3458a_read_series(n, interval_s?, save_csv?)**
        （一次会话内连测 n 点，返回逐点/统计/时长；比连调 n 次 read 省掉每次 ~0.27 s
        重连 —— 100 点 @NPLC10 约 45 s。`interval_s` 是点间间隔（主机 sleep，非精密）；
        要精密等间隔用 ks3458a_burst 的 TIMER）
        ⚡ 快路径：连接只做 `prepare_for_read`（≈0.27 s）；若表被上次会话留在 free-run
        （读数超时/错位），库会**自动做一次会话恢复并重试**——不用你手动清理
  高速采样（100k rdg/s） → ks3458a_burst(n, sample_interval_s?, dcv_range?, data_format?, save_csv?)
        `data_format="SINT"`（默认，2 字节/读数）或 `"DINT"`（4 字节/读数，
        **信号可能超过档位 120% 时必用**，手册 p.173：DINT 满量程=档位×500%）。
        跑完自动收尾并用 `PRESET NORM` 恢复 ASCII 输出（**不是** RESET）——否则后续读数会超时
  改档位/积分 → ks3458a_configure(dcv_range, nplc)：数值=**固定档**（0.1/1/10/100/1000 V），
        传 `"AUTO"` = **自动挡**（`DCV AUTO`）；另有 ks3458a_autorange(on) 只开关自动挡
        （`ARANGE ON/OFF`）。**自动挡状态只能看 `ARANGE?`**（`FUNC?` 在自动挡下仍返回固定档值）
  交流配置 → ks3458a_acv(range, band_lo?, band_hi?, sync?, nplc?)（已实测：`SETACV ANA`
        下 `TARM SGL,1` 能读出交流电压；`SYNC/RNDM` 采样法未测）
  复位（回开机配置） → ks3458a_reset(confirm=True)  ⚠ 破坏性
  ⚠ 3458A **没有** *IDN?/SYST:ERR?/*RST；**不要**用 instr_query/instr_write 对它发 SCPI

  **连不上时的第一步——分层诊断（一条命令，照做，别猜地址）**：
    `python -m keysight_3458a.preflight [--id] [--volts N] [--unstuck] [--watchdog N]`
    —— 它在**子进程**里跑、带**看门狗**（默认 30 s 超时即 kill），所以哪怕 DLL 里卡死也
    拖不垮会话。四种用法：默认=只读分层检查；`--id`=追加一条 `ID?`；`--volts N`=**最小电压
    测试**（恢复总线态 + 读 N 次，不改任何设置）；`--unstuck`=IFC+clear+RESET 救砖。
    分层结论：
    * `[1] 驱动层` `driver_missing`/`iolib_missing` → **告诉用户去装 Keysight IO Libraries Suite**
      （提示即可，**不要自己下安装包、不要提权安装**）；**不要**建议装 NI-488.2（不支持 82357B）
    * `[2] 枚举层` GPIB 为空 → 接口没带起来：**拔插 82357B + 打开 Keysight Connection Expert**
      （让它重新发现接口），等适配器名字从 "<...> Initializing" 变正常（~20-30 s）再跑一次
    * `[3] 会话层` 崩/`0xE06D7363`/`0xC0000005` → **适配器接口卡死**：杀相关进程 → 重置适配器节点
      (`usb_reset`) → 仍不行**重启整机**；期间**不要反复重试**（每次都崩，没有信息增量）
    * `[4] 应答层` viRead 无数据且 `RSRC_NFOUND`/`TMO` → **地址上没有仪器**：查 3458A 是否上电、
      GPIB 电缆两端是否插牢、面板 GPIB 地址是否=9（**这一层是物理问题，软件无能为力**）
    * `[4]` viRead **有数据** → 表被留在流数据 → 用 `--recover`（文档化恢复，不发 RESET）
    * `[5] 身份层`（`--id`）`ID?` -> `HP3458A` 才算**通路完全正常**
    * ⚠ `[5]` 若 `ID?` **回的不是 `HP3458A`，而是一个电压读数**（比方 `4.99E-01`）——
      说明表在"**只讲不听**"。两个成因今天都遇到过、且**未能完全分离**
      （2026-09-23：拔插适配器与面板按键都做过，用户回忆更像是**按了面板键**才好的），
      所以按下面顺序两条都要试，别只试一条：
      ① **看/问面板 `TALK` 指示灯**：亮 → 确诊 **Talk Only** → 让用户按
         **`Address` → `9` → `Enter`**（退出该模式、保留测量设定；或按 `Reset` 键，
         但那会一并回到开机测量配置）。
      ② 若 `TALK` 不亮 / ①做完仍这样 → **重新拔插 82357B 适配器**（适配器/接口卡死
         也会把缓冲读数当响应吐出来）→ 重新跑 `preflight --id`；仍不行则重启整机。
      **①这一态能否远程修尚未定论**：手册 p.159 只给面板处置（"press the Reset key or
      specify an address other than 31"），原理上表只听不讲时 `RESET`/IFC 送不进去；
      但 2026-09-23 那次"拔插适配器"与"面板按键"同时做过、**成因未分离**，不能据此
      断言远程一定救不回 → `ks3458a_unstick` 对这一态的**有效性待验证**，两条都试。
    **重启后的标准动作（2026-09-23 实测有效）**：重启 → 若仍连不上：**拔插适配器 + 打开
    Connection Expert** → 等初始化完 → `python -m keysight_3458a.preflight --id` 确认。

  **现场态是自动处置的（无需你手动干预，也**不要**用 reset 去"清理"）**：
    * 表可能被上次会话留在 **free-run（上电就持续吐数）**——`connect()` 自动做
      `recover()`，顺序**逐条移植参考项目** `dmm_sicl.py::open()`：
      **真 IFC** → clear → **有界** drain（≤6 轮×250 ms，绝不是无界读）→
      `TARM HOLD`/`TRIG HOLD`；不需要也不应该发 `RESET`
    * ⚡ **真 IFC 从哪来**（2026-09-23 实测）：VISA 的 `viGpibSendIFC` 在本机返回
      `-1073807257`（`VI_ERROR_NCIC`，本会话不是总线控制者）**发不出去**；而 SICL 的
      `igpibpulseifc` **返回 0 = 真的发了**。所以 VISA 传输在 IFC 失败时**自动回退 SICL**，
      并把实际通路记在 `ifc_path`/`ifc_status`（**不再静默降级成 Device Clear**）。
      这也是参考项目坚持走 SICL 的原因
    * 随后 `prepare_for_read()` 自动补 `END ALWAYS` + `INBUF ON` + **`TRIG AUTO`**：
      实测若 `TRIG?`=4(HOLD)，`TARM SGL,1` **永远不出数**（20 s 超时），补 `TRIG AUTO`
      后 0.43 s/次（NPLC=10）。这三条**不改档位/NPLC/功能**，所以是安全的读前准备
    * ⚠ **`ks3458a_burst` 是高风险操作**：2026-09-23 的适配器卡死就发生在一个 burst 上
      （DLL 内卡住 → 接口被占 → 后续所有调用失败）。现在 burst 跑在**可 kill 的 worker
      子进程**里（见下条），卡死不再拖垮 MCP，但仍建议在**专用会话**里做，跑完 `preflight --id` 验一次
    * ⚙ **3458A 的 I/O 默认跑在独立子进程里**（`keysight_3458a/worker.py` + `remote.RemoteDMM`，
      回退开关 `INSTRUMENT_KS3458A_WORKER=0`）。两个原因，**别把它改回进程内**：
      ① MCP 长驻进程里别的仪器工具会用 pyvisa 加载**系统 VISA**（`System32\visa32.dll` IVI 壳），
      与我们的 Keysight ctypes 通路**同进程必串味**——实测 `viWrite` 报 `VI_ERROR_INV_OBJECT`，
      在 MCP 里更严重：`viOpen` **访问违例**（`access violation reading 0x8`），此时 CLI 同刻却正常；
      ② 卡在 `ioGPIB` 内的调用同进程无法中断，子进程超 `deadline` 直接 `kill` → 句柄回收 → 下次自动重启
    * 若 `ks3458a_*` 报 **`access violation`** 或 **`VI_ERROR_INV_OBJECT`**：说明这条进程外隔离
      失效了（被关了 worker 开关 / worker 起不来）→ 查 `INSTRUMENT_KS3458A_WORKER` 是否为 `1`、
      看清 stderr 里的 `[3458A worker]` 记录，然后重载 MCP
    * 这些动作会打断**整条 GPIB 总线**上正在进行的采集（共享实验台注意）；本机 GPIB0
      上只有这台 3458A

  **3458A 重要坑清单（写脚本/集成前逐条过；详版见 `keysight_3458a/README.md` 与
    `docs/3458a_manual_verification_20260923.md`）**：
    1. **非 SCPI**：`ID?`（无 `*IDN?`）、`ERRSTR?`（无 `SYST:ERR?`）、`RESET`（无 `*RST`）、
       读数是 `TARM SGL,1`（不带问号）；**不要**用 `instr_query`/`instr_write` 对它发 SCPI。
    2. **串尾必须 LF**：CRLF 结尾会让 3458A **不应答**（实测超时）。库已强制 `"\n"`；
       自己写 socket/串口/GPIB 时同样要注意。
    3. **`TRIG HOLD` 下 `TARM SGL,1` 永不出数**：读前必须 `TRIG AUTO`（库自动补）。
    4. **free-run（上电吐数）**：恢复 = clear/IFC + **有界** drain + `TARM/TRIG HOLD`；
       无界 drain 实测 80~91 s。库在读数失败时自动恢复一次并重试。
    5. **换档/换配置后首读数必须丢弃**（建立时间+自校准）；库自动做。
    6. **突发会改配置**（`PRESET DIG`：数字档/DCV/内存关/**SINT 输出**）→ 必须收尾 +
       `PRESET NORM` 恢复，否则 ① 继续流数据卡死下条命令 ② SINT 无换行使 ASCII 读数超时。
       `PRESET NORM` 手册原文"similar to RESET"——**会复位档位/NPLC**（不是 RESET 命令，
       但脚本里跑完突发要显式 configure 回自己的设定）。
    7. **码表易错**：`MFORMAT?`=4 是 **SREAL**（SINT=2）；**`FUNC?` 不能判自动挡**（看 `ARANGE?`）。
    8. **SINT 只适用 ≤120% 档位**；>120% 必须用 **DINT**（满量程=档位×500%）。
    9. **`ERRSTR?` 每次读并清除"最低置位"一位**：要反复查到 `0,"NO ERROR"`；它也是
       "设备接受了这条命令"的**唯一证据**（写操作无回读通道）。
    10. **APER 与 NPLC 是同一积分时间**（后设者生效）；NPLC 10 @50 Hz = 0.2 s → 0.43 s/读数。
    11. **占用与并发**：同一地址两个会话会**静默串台**（实测互相读到对方响应）。库已接
        `common/session_lock`：别人在用同一地址时 `connect()` **直接拒绝**并列出 pid/kind；
        等对方释放，**确认对方已死**才 `connect(force=True)`。
        ⚠ **咨询锁 ≠ 资源释放**：锁文件过期/被清空**不代表** GPIB 句柄释放。若出现
        `viOpen` 抛 `0xE06D7363`（甚至进程 `0xC0000005` 访问违例）、且**杀掉所有可疑进程 +
        拔插适配器后仍失败** → 那是**适配器接口层坏了**：先杀相关进程 → 重置适配器节点
        （`usb_reset`）→ 仍不行就**重启整机**（`pnputil /restart-device` 也会这么要求）。
        排查期间**不要反复重试**（每次都会崩，没有意义）。详见
        `docs/3458a_wedge_postmortem_20260923.md`。
    12. **总线影响**：IFC/Device Clear 影响**整条 GPIB 总线**（本机 GPIB0 上只有这台表）。
    13. **上限/性能**：`NRDGS` ≤ 16777215；VISA 超时最小粒度 ≈2 s（所以 drain 是慢路径）；
        快路径连接 0.27 s、单次读数 0.43 s（NPLC 10）。
    14. **命令白名单**：只有 `keysight_3458a/commands.py` 里的命令；新增命令先登记出处
        （手册页码/实测留痕），见铁律 1。
    15. **"每条命令都回一个电压读数"（表不听、只讲）** —— 有**两个**可能原因，两个都要试：
        ① **Talk Only 模式**（前面板 `ADDRESS`=31；手册 p.159：`TALK` 灯亮、地址存连续
           内存、**断电不丢**）：表**根本不听**，`RESET`/IFC 都送不进去，**只能前面板**：
           **`Address` → `9` → `Enter`**（退出 Talk Only，保留测量设定），或按 `Reset` 键
           （会一并回到开机测量配置）。**先看 `TALK` 灯**判断是不是这一条。
        ② **适配器/接口卡死**：`ioGPIB` 卡死后把缓冲里的读数一直当响应吐出来——
           处置：**重新拔插 82357B**（必要时重启整机）→ 跑 `preflight --id` 复验。
        ⚠ 2026-09-23 那次两个动作都做过、**无法分离**（用户回忆更像按键起效）→ 不要只试一条。
        ⚠ `ks3458a_unstick`（IFC+RESET）对 **Talk Only 的有效性待验证**：手册只给面板
        处置，原理上表只听不讲时收不到 `RESET`；但 2026-09-23 两成因未分离，不能断言
        远程一定无效。`preflight --id` 会识别这一态并提示按哪个键。

  **在脚本里集成 3458A（两种方式，二选一）**：
    ① **直接用库（脚本推荐）**：`from keysight_3458a import DMM3458A`
       → `with DMM3458A("GPIB0::9::INSTR") as d: d.read_dcv()`
       适合批量采集/长脚本/需要精细控制（突发、逐点 CSV、异常处理）；库里已接会话锁。
    ② **走 MCP（与 AI 共用一把锁、复用安全门）**：起 `mcp_instruments/server.py` 子进程，
       用官方 SDK（`mcp.ClientSession` + `mcp.client.stdio.stdio_client`）调 `ks3458a_*` 工具，
       返回值是 **JSON 文本**。适合"想让 AI 和自己串行用同一台表"；代价是每脚本多 ~1 s 启进程。
    两条路都在示例里写好了（可直接抄）：`TEST_SCRIPTS/ks3458a/example_script_integration.py`
       · `--dry-run` **不连设备**，只打印将要下发的命令序列（写脚本前先跑这个）
       · 默认 = ①库；`--via mcp` = ②；`--burst` 追加一段 100 点 SINT 突发（会改配置并自动恢复）

DHO 示波器 → dho_status / dho_measure_item / dho_channel / dho_timebase / dho_trigger
  （⚠ DHO 不在本实验台：读路径同共享内核，**写路径未实机验证**）

MHO900 示波器（MHO984D 实测基准）：
  全量状态 → mho_status（每通道附 `center_v = −offset` 与 `window_v`；采样率随开启通道数下降：
    1~2ch 4GSa/s、3~4ch 1GSa/s）
  读测量值 → mho_measure_item（单信源 VPP/VMAX/VAVG/VRMS/FREQuency…；
    双信源延迟/相位 RRDelay/RFDelay/FRDelay/FFDelay、RRPHase/RFPHase/FRPHase/FFPHase 需 ch2）
    **结论要用均值 → samples=5**（返回 mean/min/max/stddev，无效读数单独计数）
  设档位/偏置/耦合/探头 → mho_channel（**先 scale 后 offset 已在工具内固定**；返回
    requested/actual/adjusted/reasons/window；通道 OFF 时会自动先开——OFF 下写入被静默忽略）
  把某通道波形弄"合适居中" → mho_fit_channel(ch)（只动该通道；平直信号判 flat 不猜档位）
  改时基/触发 → mho_timebase / mho_trigger（**都是全局项**，共享实验台上会改变他人观察）
  判断削顶/居中/有无波形 → mho_screenshot（返回 PNG 路径，**直接 Read 看图**）
  读波形 → mho_get_waveform（NORMal 1~1000 点最常用；RAW 内存波形需先 mho_acquisition("stop")）
  无波形且确认"信号简单周期 + 无他人在用通道" → mho_autoset(confirm=True)（**全局破坏性**）

DG832 信号源（RIGOL DG800 系列）：
  **强制流程（顺序不可变）**：dg_status 查现状 → dg_protect 开电压保护（state=True 且 high>low）
    → dg_set_wave/dg_set_param 设参数 → dg_output(on,confirm=True) 开输出 → dg_check_error
  看现状 → dg_status（波形/频率/幅度/偏移/输出/负载）；查保护配置 → dg_get_protect(ch)
  设波形 → dg_set_wave（省略参数=保持当前值；amp/offset 需已开保护，否则 protect_required）
  单参数 → dg_set_param（freq/amp/offset/phase/load；设备钳制时返回 note）
  DC 电平 → dg_set_dc（返回切换前快照 restore，切回时**显式**传参）
  扫频 → dg_sweep / dg_sweep_trigger（仅 sine/square/ramp/user）
  开关输出 → dg_output（**开/关都需 confirm=True**）
  诊断 → dg_query（纯查询 SCPI）/ dg_check_error

电源（DH1766）→ **psu_status（先查！含安全 warnings）** / psu_mode / psu_set_mode / psu_output / psu_power_cycle
```

### 资源地址：专用工具的 `resource` 默认不传

仪器地址**不是固定资产**（DHCP/换网段/换 USB 口/串口号漂移），**不要**在任何地方
把具体 IP 当成设备资源记住或写死。专用工具的 `resource` 参数默认省略，server 端按
`显式入参 > 环境变量 INSTRUMENT_<KIND>_RES > 用户配置 devices.json > 上次成功缓存 > 自动发现`
解析（`common/resolver.py`）。

- 工具报"未确定 XX 的资源地址"→ 先跑一次 **`instr_discover`**（结果按 `*IDN?` 自动记住），
  再重试原工具即可；也可让用户设 `INSTRUMENT_<KIND>_RES` 或写 `devices.json`
  （本机默认地址：`python mcp_instruments/config_cli.py set <kind> <resource>`）。
- **地址一律用完整 VISA 资源串**（`TCPIP0::…::inst0::INSTR` / `USB0::…::INSTR` /
  `ASRL5::INSTR` 同一形态，不区分传输方式），**不要自己拼 `IP:端口`**——协议/端口/
  参数因设备而异，拼错就是对未知设备发 SCPI。只有用户明确给出某台设备的 IP/host 时，
  才用 `config_cli.py set <kind> <host>` 让工具探测协议并核对身份后落库。
- 工具报"**地址校验失败**"→ 该地址上的设备 `*IDN?` 与目标不符（DHCP 把旧 IP 分给了
  别的设备）。**不要重试硬连**，先 `instr_discover` 重新定位，或让用户更正 `devices.json`。
- `instr_discover` 的返回体五组字段：`lan` = 网段扫描结果（TCP 预筛 + 多协议 `*IDN?`，
  键=资源串、值=IDN）；`visa` = VISA 资源列表（USB/GPIB/串口均自动探测 `*IDN?`，
  串口被占用时给占用提示、空闲则探测后立即断开，约 2s/口）；`resolved` = 当前解析表；
  `recognised_now` = 本次按 `*IDN?` 识别并写入缓存的设备；`psu_local_restored` =
  是否已把 DH1766 面板控制权归还现场（该电源任何远程会话都会进 REM，见
  `dh1766_control/docs/EXPERIENCE.md` §3.1）。
- `cidr` 参数可选（如 `'10.0.0.0/24'`）；**省略时扫本机所有接口所在的私网网段**
  （`cidrs` 字段列出实际用到的网段）——多网卡 / 挂 VPN / 代理 TUN 开启时都能覆盖到
  仪器所在的那张网卡（TUN 只加虚拟网卡、不改物理网卡地址）。只想扫特定网段时显式传
  `cidr`。
- 只有通用工具 `instr_query` / `instr_write` 必须显式给 `resource`（面向任意设备，不能猜）。

### 3458A 脚本收尾规范：`ks3458a_snapshot` → 测量 → `ks3458a_restore`（2026-10-08 定）

**为什么**：单次读数配方 `TARM SGL,1` 触发一次后自动回 HOLD，`close()` 还会补
`TARM HOLD`——脚本测完前面板就停在 **MRNG + 冻结读数**（用户以为仪器坏了）。
MRNG = manual range 指示符（手册：不在自动挡时点亮），不是故障。

**标准三步**（try/finally）：

```
1. 开始前  ks3458a_snapshot()          # 测量配置快照（FUNC/RANGE/NPLC/ARANGE/AZERO）
2. ...测量...（read/configure/set_range 随意）
3. finally ks3458a_restore(snap=<第1步的 result>)
          # 配置按快照还原 + 触发态无条件 AUTO（面板连续刷新）+ ARANGE ON（灭 MRNG）
```

设计裁定——**为什么触发态不按开始状态恢复**：开始前若是上次烂尾的 HOLD，
"按原样恢复"会把坏状态永久化（2026-10-08 现场正是这样）。触发态决定面板死活，
属交接态而非用户设置 → 无条件 AUTO；**测量配置**（特意设置的档位/NPLC）才走快照还原。

一次性的快速交还（不想快照）：`ks3458a_release`。脚本侧直连库等价物：
`d.snapshot_config()` / `d.restore_config(snap)` / `d.release_to_front_panel()`；
`d.close(release=True)`。⚠ 释放后**不要再读数**（单次触发会把 TARM 打回 HOLD）。

### 3458A 的两条通路怎么配（`kind=ks3458a`）
3458A 挂在 GPIB 上，**不是 LAN 设备**（扫网段对它没有意义）。通路按资源串自动选：

| 通路 | 配置值 | 何时用 |
|---|---|---|
| **本机 GPIB（推荐，默认）** | `... set ks3458a "GPIB0::9::INSTR"` | 本机 82357B USB/GPIB。库会自动走 **Keysight VISA 核心（`ktvisa32.dll`）+ 预加载 `ioGPIB.dll`/`ioGpibIntfc.dll`**——这是本机 64 位 Python 唯一可行组合（系统默认 `visa32.dll` 会 `VI_ERROR_LIBRARY_NFOUND`） |
| 本机 SICL | `... set ks3458a "sicl:gpib0,9"` | Keysight IO Libraries 装了、且 SICL 可用时的备选（EmoeCalibrator 现场用的就是它） |
| 远端 VISA server | `... set ks3458a "visa://<host>/GPIB0::9::INSTR"` | 设备挂在另一台机器的 VISA server 上（不走本地 Keysight 通路） |

`sicl:` 前缀被解析层视为**完整资源串**（不会当成裸主机名去探测网段）；不带任何配置时
`ks3458a_*` 工具会按 `本机 GPIB0::9::INSTR → sicl:gpib0,9` 依次探测（`find_3458a()`），
**不会**扫描网段。身份校验走连接后的 `ID?`（返回含 `3458`，3458A 没有 `*IDN?`）。
**连不上先跑 `python keysight_3458a/driver_check.py`**（见上文"驱动预检查"）。

## 一.五、通用护栏工具（新设备零代码接入）

有专用库的设备优先用专用工具；以下用于骨架设备（如 emoe）、临时设备、
或库尚未覆盖的能力。命令语法必须先对照该设备手册/`commands.py`（铁律1），
**禁止猜测**——设备对不认识的命令静默不应答→超时（如 SDG 不支持
`C1:BSWV WVTP?` 单键查询，只支持整查 `C1:BSWV?`，见 sdg_control/commands.py）。

| 工具 | 用法要点 |
|---|---|
| `instr_query(resource, cmd, timeout_ms?)` | cmd 必须含 `?`；**问号后允许带参数**（`:MEASure:ITEM? VPP,CHANnel1`、`:TRIGger:EDGE:LEVel? MAX`、`:VOLT:DC?`）；**可用 `;` 串联多段纯查询**（如 `:CHANnel4:DISPlay?;:CHANnel4:SCALe?`），但夹带写/复位/锁定会被拒；只读不留痕 |
| `instr_write(resource, cmd, readback_cmd?, confirm, timeout_ms?)` | **必须 confirm=True**；写前 drain、写后 SYST:ERR?、readback_cmd 给定即自动回读（铁律2/3）；每次调用含拒绝均落盘 `TEST_DATA/common/mcp_scpi_audit_*.jsonl` |

护栏语义：查询判据＝**逐段检查每段都是查询单元**（命令头以 `?` 结尾、问号后可带参数）——多段纯查询放行，夹带写命令即拒（SCPI 里 `;` 是同消息内的命令单元分隔符、设备逐个执行，实测写单元会生效）；原写法偶有歧义（不是"整条以 ? 结尾"——SCPI 允许问号后带参数）；复位/存储覆写类（`*RST`/`*SAV`/`*RCL`/`:SYST:RES|FACT|PRES`，长短形式均拦）
一律 `forbidden` 拒绝，confirm 也不放行（复位需显式授权场景走测试脚本）；
设备无响应有硬超时看门狗（≥30s），离线资源不会冻结 MCP；
串口(ASRL)按 9600 波特。

### 新仪器渐进式接入流程（compact 面口径，五步）

**目标**：新设备从"发现"到"落库成专用工具"，每步都有护栏、每步留痕——不跳步、
不猜命令（铁律 1：语法必须对照设备手册）。

```
第 1 步 发现资源
  instr_devices(full=true)        # 全量扫描（贵：本机实测 355.9s，会占住执行器）
  # 常见捷径：instr_devices()（瞬时，读缓存/配置）先看已知地址；只有新设备才 full=true
  # 串口设备（ASRL 编号漂移最频繁）一律先发现再操作
  → 拿到 VISA 资源串（如 TCPIP0::192.168.31.x::inst0::INSTR）

第 2 步 身份确认 + 只读跑通（零代码，用通用工具）
  instr_query(resource, "*IDN?")           # 身份——确认没连到别的设备
  instr_query(resource, "<手册里的查询>")   # 逐条跑通手册命令，确认语法
  # 3458A 例外：不是 SCPI 表（ID?/ERRSTR?），禁用这两条通用工具

第 3 步 写命令验证（有护栏）
  instr_write(resource, cmd, readback_cmd=..., confirm=True)
  # 写前自动 drain 错误队列、写后逐条查 SYST:ERR?、回读比对——三步齐
  # 拒绝/执行都落盘 TEST_DATA/common/mcp_scpi_audit_*.jsonl

第 4 步 落库（命令有了出处之后）
  a. <device>_control/commands.py   # 命令常量 + 手册页码出处（零猜测的自证）
  b. <device>_control/<device>.py   # 库方法（连接→操作→关闭；写后回读带容差）
  c. mcp_instruments/server.py      # 可选：@device_tool() 暴露 MCP 工具
  d. instrument_runtime/catalog.py  # ⚠ 必须登记风险分类——未分类的服务起不来
  e. skills 的速查表 + 决策树      # 工具行为与坑写进 skill（agent 才会正确用）

第 5 步 防回归
  python TEST_SCRIPTS/common/audit_all_commands.py        # 命令审计（出处核对）
  python TEST_SCRIPTS/common/regression_offline.ps1       # 全套离线回归
```

**渐进式的含义**：第 1-3 步**零代码**（通用工具即可操作新设备），第 4 步才写代码——
只有被证明正确的命令才进库。跳步的代价：猜语法 → 设备静默不应答 → 超时白等；
猜分类 → 服务起不来（宁可拒绝启动，见 `_register_runtime_operation` 的纪律）。


## 二、参数语义速查

| 工具 | 参数 | 语义 |
|---|---|---|
| sds_auto_scale | ch=1-4；use_autoset | **use_autoset=True 破坏性**（重置所有通道），仅简单周期信号+无其他已调通道时用；无信号/小信号时逐档重试最长约 60s，最终优雅报错 |
| sds_measure | item | SIMPle:ITEM 表 **51 项全支持**：PKPK/MAX/MIN/AMPL/TOP/BASE/LEVELX/CMEAN/MEAN/STDEV/VSTD/RMS/CRMS/MEDIAN/CMEDIAN/OVSN/FPRE/OVSP/RPRE/ULOWer/PER/FREQ/TMAX/TMIN/PWID/NWID/DUTY/NDUTY/WID/NBWID/DELAY/TIMEL/RISE/FALL/RISE20T90/FALL80T20/CCJ/PAREA/NAREA/AREA/ABSAREA/CYCLES/REDGES/FEDGES/EDGES/PPULSES/NPULSES/PACArea/NACArea/ACArea/ABSACArea；ch=1-4；无有效读数（如无信号测频率）超时后返回 device_error，**属正常现象** |
| sds_get_waveform | ch, points=50000, save_csv | 读通道波形（电压+时间轴，**FFT 交叉验证可信**：interval 与 `ACQ:SRAT?` 一致、FFT 主频与设备硬件测量吻合）：返回摘要（点数/时间窗/Vpp/interval/档位），save_csv=True 存 CSV 到 TEST_DATA/common/ 并返回路径；**不返回完整数组**（防上下文爆炸）；分析频率用 FFT/自相关，朴素过零对调幅信号会误判 |
| sds_screenshot | resource（可省略） | 截屏存 PNG 并返回路径，**可直接 Read 读图**；看波形形态/削顶/居中/菜单/光标/测量栏；无视觉能力时用 analyze_screen 像素分析兜底 |
| sds_meas_threshold | source, thr_type, absolute, percent | 测量阈值源/类型/绝对值/百分比（边沿判据基础，手册 p.183-185）|
| sds_meas_gate | on, ga, gb | 测量门限：只统计 GA~GB 窗口内波形（p.179-180）|
| sds_meas_statistics | on, max_count, histogram, reset, slot, which | 统计开关/次数/直方图/重置；给 slot 查该槽统计（p.166-174）|
| sds_meas_dtime | index, edge1/2, slope1/2, threshold1/2 | 延迟测量 ΔTime 配置（p.176-178）|
| sds_meas_display | rdisplay, style, linenumber, strategy, astra_base/top | 结果显示样式/统计模式/幅值策略（p.174-181）|
| sds_measure_phase | src_a, src_b | 双通道相位差（度）= B 相对 A（PHA）；用后自动清槽恢复模式；两通道都要有完整周期（C1 无信号时正确报 device_error）|
| sds_measure | 无信号测 FREQ | 超时报 device_error（正常现象，非故障） |
| sdg_counter | on? | 内置频率计 FCNT（SDG2000X；SDG7000A 才是 :SENSe:COUNTer:*）；返回 FRQ/PW/NW/DUTY/FRQDEV 等；无信号 FRQ=0HZ |
| sdg_set_wave | wvtp | SINE/SQUARE/RAMP/PULSE/NOISE/DC；amp_v 高阻下即 Vpp |
| sdg_output | ch, on, **expect_load**, confirm | 输出开关；**expect_load 必填**（HZ/50Ω，仅校验，不符拒绝）；**开/关都需 confirm=True**（关闭可能打断测试/他人实验）|
| dmm_nplc | value? | 电压 DC 积分时间 NPLC（0.02/0.2/1/10/100，越大越准越慢）；无参查询，有参设置后回读 |
| dmm_measure | function | volt_dc/volt_ac/curr_dc/curr_ac/res/fres/cont/cap/diod/freq |
| dmm_configure | range_v | 设定量程后 :CONF? 回读滞后一拍，以实测为准 |
| ks3458a_status | resource? | `ID?`/`ERRSTR?`/`TEMP?` + **`device` 设备回读**（`FUNC?`/`RANGE?`/`NPLC?`/`APER?`/`TARM?`/`TRIG?`/`NRDGS?`/`INBUF?`/`END?`/`MEM?`/`AZERO?`/`OFORMAT?`/`MFORMAT?`/`ISCALE?`——2026-09-23 真机实测 + **手册逐条核对**，`TARM/TRIG/END/INBUF/OFORMAT/MFORMAT/AZERO` 已按手册码表**解码**成 `4(HOLD)`/`1(ASCII)`/`4(SREAL)` 形式）+ `tracked`（本会话**下发过**什么，与回读分开报）。`TEMP?` = 内部温度，单位**摄氏度**（手册 p.37/50；实测 37.0 / 36.9） |
| ks3458a_read / ks3458a_read_avg / ks3458a_read_stats | n≤1000 | 单次 DCV（`TARM SGL,1`）/ n 次平均 / `{n,mean,stddev,min,max}`（样本标准差）。读数非数值按 device_error 报，**不返回 0 兜底** |
| ks3458a_read_series | n≤1000, interval_s?, save_csv? | **一次会话内连测 n 点**（重复测量主用）：返回 `{summary:{n,mean,stddev,min,max,duration_s,per_reading_s}, values?}`（n≤50 内联逐点，更大只回摘要；`save_csv=True` 落带 unix 时间戳的 CSV）。点间 `interval_s` 为主机 sleep（非精密时序）。单点失败自动恢复重试一次，仍失败则报错并附已采点数 |
| ks3458a_burst | n, sample_interval_s?, dcv_range?, data_format?, save_csv? | 100k rdg/s 二进制突发（`PRESET DIG`+`MFORMAT/OFORMAT`+`MEM OFF`+`NRDGS`+`TRIG AUTO`+`TARM SYN`+`ISCALE?`）。`data_format="SINT"`（2 字节/读数，读 2n+2 字节）或 `"DINT"`（4 字节/读数，读 4n；**信号可能超档位 120% 时必用**，手册 p.173：DINT 满量程=档位×500%）。跑完**自动收尾**（`TARM/TRIG HOLD`+clear+有界 drain）并 `PRESET NORM` 恢复 ASCII 输出（非 RESET）。n 上限=设备 16777215（手册 p.207）。返回**摘要**；`save_csv=True` 落 `TEST_DATA/ks3458a/`。⚠ **改设备配置** |
| ks3458a_configure | dcv_range, nplc | **固定档 vs 自动挡**（手册 p.183-184）：`dcv_range` 传数值（0.1/1/10/100/1000 V）=**固定档**，传 `"AUTO"`=**自动挡**（`DCV AUTO`）。10V 档可到 12 V（手册 p.136：120% of range），但选档按 1.1 倍余量（保守）。**换档后自动丢首读数**（建立时间+自校准）。返回体带 `device_readback`（`FUNC?`/`RANGE?`/`ARANGE?`/`NPLC?` 实测回读） |
| ks3458a_acv | range, band_lo?, band_hi?, sync?, nplc? | `ACV`/`SETACV ANA|SYNC`/`ACBAND <lo>,<hi>`（带宽需成对给）。`SETACV SYNC` 用于 <10 Hz、`ANA` 用于 >10 Hz。✅ 2026-09-23 实测：`ANA` 下 `TARM SGL,1` 能读出交流电压（4.11 mV AC）；`SYNC/RNDM` 采样法未测 |
| ks3458a_autorange | on | `ARANGE ON` / `ARANGE OFF`（手册 p.160）；**只开关自动挡**，不碰档位数值/NPLC。回读看 `device.arange`（`1(ON)`/`0(OFF)`）与 `device.autorange`（`FUNC?` 在自动挡下仍返回固定档值，**别用它判自动挡**） |
| ks3458a_reset | confirm | `RESET`+`END ALWAYS`+`INBUF ON`；**回到开机测量配置**（= 手册 p.26 Table 5 上电状态：`DCV AUTO`/`NPLC 10`/`END OFF`/`INBUF OFF`/`MFORMAT SREAL`…）。MCP 连接默认**不**重置仪表，这是唯一重置入口 |
| ks3458a_unstick | confirm | **救砖**（顺序逐条移植参考项目 `dmm_sicl.py::open()`）：`IFC` → clear → `TARM/TRIG HOLD` → **`RESET`** → clear → `END ALWAYS`/`INBUF ON` → 读 `ID?`。用于"表能听但一直吐数 / 一切调用超时"这类**free-run / 收尾失败**场合。返回 `ifc_path`（`visa`/`sicl`/`none`）+ `idn` + 一次读数。⚠ **破坏性**（`RESET` 回开机配置）；⚠ **对 Talk Only 的有效性待验证**（手册只给面板处置 `Address` → `9` → `Enter`；2026-09-23 两成因未分离，不能断言远程一定无效） |
| （3458A 通用） | — | **禁用** `instr_query`/`instr_write` 操作 3458A——那两条面向 SCPI，而 3458A 是 `ID?`/`ERRSTR?`/`RESET`/`TARM SGL,1` 那套；库只允许白名单命令（见 `keysight_3458a/docs/COMMANDS_3458A.md`） |
| dho_measure_item | item, ch, ch2?, samples? | RIGOL 长名：VPP/VMAX/VAVG/PERiod/FREQuency...；samples>1 给均值统计；无值分类报因（suspicious/hint）|
| dho_channel / dho_timebase / dho_trigger | 同 mho_* 同名工具 | DHO 的设置类工具（同一套内核语义）；⚠ DHO 不在本台，未实机验证 |
| mho_measure_item | item, ch, ch2?, samples?, rails? | 手册 3.17.2 表。**单信源 30 项**：VMAX/VMIN/VPP/VTOP/VBASe/VAMP/VAVG/VRMS/OVERshoot/PREShoot/MARea/MPARea/PERiod/FREQuency/RTIMe/FTIMe/PWIDth/NWIDth/PDUTy/NDUTy/TVMAX/TVMIN/PSLewrate/NSLewrate/VUPPer/VMID/VLOWer/PVRMs/PPULses/NPULses/PEDGes/NEDGes/ACRMs；**双信源 8 项**（需同时给 ch2）：RRDelay/RFDelay/FRDelay/FFDelay（延迟）、RRPHase/RFPHase/FRPHase/FFPHase（相位，四组合=先 A 后 B 的沿型）；ch=1-4。**samples>1**（≤200）连读给 mean/min/max/stddev/count/invalid（单次读数抖动明显：现场同状态连读得 7.4747/7.4749/7.4749 V，**结论要用均值**）；无有效值时 `suspicious` ∈ channel_off/off_screen/near_edge/few_edges/no_signal + `hint`；返回带 `probe_x`（探头比 ≠1 时幅度类读数是**探头端**电压）；**`rails=True`** 额外读**顶轨/底轨**（VTOP/VBASe）并**上下分别**判断贴边——`edges_touching`（["top"]/["bottom"]/两者）+ 各自 `hints`（顶贴→offset 调更负；底贴→offset 调更大，方向相反故分开报）；**未标定格数的家族不做贴边判定**（`note` 说明，不猜）|
| mho_channel | ch, scale?, offset?, coupling?, probe?, display? | **垂直设置**：固定 **scale→offset** 顺序（改 scale 会等比缩放 offset）；通道 OFF 时自动先开（OFF 下写入被静默忽略）；偏置超量程 → `adjusted`+`reasons`；返回 `window`=[bottom, top]（**中心 = −offset**）。只动指定通道。**偏置量程上限随档位变**（2026-09-16 实机阶梯：0.05 V/div→±1 V、0.1~0.2→±10 V、0.5~2→±20 V、5~10→±100 V）——小档位下"居中"可能做不到，要先抬档位（证据 `docs/tool_optimization_20260915.md` §P1-3）|
| mho_timebase | scale?, offset? | 时基（s/div、位移）**两者都回读**；**全局项**。屏内 <2 个周期时频率读不到（20 µs/div 看 5 kHz 读不到、100 µs/div 立刻读到 4.9993 kHz——是窗口不够，不是精度问题）|
| mho_trigger | source?, level?, slope?, mode?, sweep? | 边沿源/电平/斜率 + 模式/扫描（枚举对照手册）；**全局项**；电平受限时 reasons 带该通道窗口范围 |
| mho_fit_channel | ch, occupancy=0.7, margin=0.08, max_iter=12 | **单通道自动定标/居中**：判据 ①可测（读数非 9.9E37）②不贴边（margin 带内）③占屏率 ∈ [0.4, 0.9]（目标 occupancy）；算法 通道必须已开 → 不可测/贴边则沿 1-2-5 逐档放大 → `scale = span/(occupancy×8 格)`、`offset = −中心` → **先 scale 后 offset** 写回 → 重测验证；平直/无信号 → `flat=true` 且**保持档位不猜**（按均值居中）；超量程/未收敛 → `ok=false`+`reason`+`trace` 证据。**不是 autoset**（只动一个通道）|
| mho_get_waveform | ch, points=1000, mode, fmt, save_csv | NORMal **最多 1000 点**（手册 3.28.4；超限报 param_validation）；RAW/MAXimum 可多但 RAW 必须已 STOP；fmt=BYTE/WORD/ASCii；voltage=(raw-YORigin-YREFerence)*YINCrement；RAW xinc 与采样率自洽（实测 5e-10s @2GSa/s） |
| mho_screenshot | resource? | `:DISPlay:DATA? PNG` 原生 PNG（**无需转码**），存 TEST_DATA/mho/ 并返回路径，可直接 Read 读图 |
| mho_acquisition | action=run\|stop\|single\|force | **stop 会冻结采集**（共享实验台上可能打断他人观察）；RAW 读内存波形前必须 stop，读完记得 run |
| mho_autoset | confirm | **全局破坏性**：重置**所有**通道档位/时基/触发；仅在"信号简单周期 + 无其他已调通道"时用 |
| dg_output | ch, on, confirm | 开/关都需 confirm=True；打开前需已开保护（库内联锁 protect_required） |
| dg_protect | ch, high, low, state | **设幅度/偏移或开输出前必须先开**（state=True 且 high>low）；越界设置报 protect_range，不静默超压 |
| dg_set_wave | shape, freq, amp, offset, phase, sample_rate | 各波形 `:APPL` 参数模板不同（DC/DUAL/PRBS 无 phase、NOISE/RS232 无 freq、SEQ 首参是采样率），库已按模板生成；频率上限随波形变（square/pulse 10MHz、ramp 1MHz）|
| dg_set_param | param, value | freq/amp/offset/phase/load；写后回读，设备钳制时 note 提示 |
| psu_status | — | **电源状态总览（操作前先调）**：三路电压/电流/功率/设定/OVP/OCP/输出/模式/耦合 + **safe/warnings** 安全检查（原 measure 与 pre_check 已并入）|
| psu_power_cycle | ch, expect_mode, cycles=1, off_delay_s=1.0, on_delay_s=1.0, confirm | 上下电循环（关→延迟→开→延迟）；**confirm 必填**（授权同输出开关）；放电不足时调大 off_delay_s（电容残留需 ≥6s）|
| psu_mode / psu_set_mode | mode | **操作电源前先查模式**：NORM/TRAC/SERI/PARA；TRAC 下 CH2 跟随 CH1 输出负压（非故障，手册§3.8）；切换前输出必须全关（库内强制）|
| psu_output | ch, on, **expect_mode**, confirm | 单通道输出开关；**expect_mode 必填**（仅校验，不符拒绝并回传实际模式）；**开/关都需 confirm=True**（关闭可能中断供电）|

## 三、安全门

| 工具 | 门 | 说明 |
|---|---|---|
| sds_shutdown | confirm=True | 设备离线需面板手动开机 |
| sdg_output（开/关） | confirm=True | 开=真实信号；关=可能打断测试/他人实验 |
| mho_autoset | confirm=True | 全局破坏性：重置所有通道/时基/触发（多信号台面慎用） |
| dg_output（开/关） | confirm=True | 开=真实信号输出；关=可能打断测试/他人实验 |
| dg_protect → set_wave/output | 库内联锁 | 未开有效电压保护时设 amp/offset 或开输出一律被拒（protect_required） |
| psu_output（开/关） | confirm=True | 开=真实电压；关=可能中断供电 |
| ks3458a_reset | confirm=True | **回到开机测量配置**（`RESET`+`END ALWAYS`+`INBUF ON`）——档位/NPLC/功能/触发全变；共享实验台上会把表从别人设置的档位踢回默认 |
| ks3458a_burst | 无 confirm，但有副作用 | 取数本身只读，但 `PRESET DIG` **改设备配置**（数字档预设 / 功能切 DCV / 内存关闭）；跑完要还原请显式 `ks3458a_configure` 或 `ks3458a_reset` |
| ks3458a_*（全部） | 无 confirm，但会打断 | 连接即做会话恢复（IFC/Device Clear + 有限 drain + TARM/TRIG HOLD）：SICL 的 IFC 影响**整条 GPIB 总线**，别人正在采集时先用 `ks3458a_status` 之外的渠道确认安全 |
| （未暴露）| — | 复位类命令一律不可用 |

### 输出关断的授权确认（重要纪律）

`confirm=True` 不是"我知道要关"就填——它代表**已获得关断授权**。满足以下之一才可关断：

1. **用户本轮明确要求关闭**该输出（如"把信号源关掉"），或**明确要求做上下电/上下电循环**；
2. **用户明确声明独占使用**："只有你在用这块供电/供信号的板子"、"没有别人在用"。

不满足时**不要关断**：实验台可能是共享的（同一电源可能给别人的板子供电、
同一信号源可能接在别人的测试链路里），贸然断电会打断正在进行的测试/实验。
需要关断但无授权时，**先向用户确认**再执行。

同理，`psu_output`/`sdg_output` 的 `expect_mode`/`expect_load` 也必须在**先查询
确认**后填写（`psu_mode`/`sdg_status`），不能凭记忆或猜测填——填错会被拒绝，
但正确做法是查了再填。

### 禁止远程锁定命令（面板保护）

**不要发送远程锁定类命令**：`:SYSTem:REMote ON`（SDS 实测：禁用触摸屏、前面板
按键和其他外设，界面显示 "Remote"）、串口/GPIB 通用的 `SYST:REM` / `SYST:LOCK`、
以及 **DH1766 的 `SYST:RWL`**（面板 Lock 键不可切回本地，需 `SYST:LOC` 恢复）
和标准形式 `:SYST:COMM:RLST <state>`（RWL 值同样锁面板）。

理由：实验台是共享的，锁定面板会妨碍人工操作（现场调试/检查往往需要直接按面板）。
SDS 的远程锁定**不是连接自动触发的**，是显式命令——所以只要不发就永远不锁
（2026-09-09 实测：连接+查询不改变 REM 状态；残留 ON 多来自 Web/noVNC 控制界面）。

- MCP `instr_write` 已黑名单拦截（`error_type=forbidden`，confirm=True 也不放行）；
  2026-09-13 补齐 `SYST:RWL` / `:SYST:COMM:RLST` 缺口；
- **纯查询放行**（`SYST:REM?`/`SYST:COMM:RLST?`）——纯查询不改变锁定状态，用于诊断；
- 若发现面板已被锁（`SYST:REM?` 返回 ON），提示用户用面板或 Web 界面退出远程模式。

### DH1766 特例："一连就进远程模式"（2026-09-13 实测）

DH1766 与 SDS 不同：**任何远程会话都会把电源置为 `REM`**——新建会话第一条命令查
`SYST:COMM:RLST?` 即返回 `REM`（手册写的 `SYST:COMM:RLST:STAT?` 在本机 V0.1.4.3
**无响应**，不是"返回空串"）。这是设备行为，不是锁定命令被误发。

- 发 `SYST:LOC` 立即回到 `LOC`（只交还面板控制权，**不影响输出/电压/模式**）；
- MCP 电源工具已内置：每次调用收尾自动补发 `SYST:LOC`（`server.py::_psu_close`），
  所以 AI 用过之后现场面板仍可用；
- 用 `psu_status` 前不必纠结 REM，它会如实回报当前模式（TRAC/SERI/PARA/NORM）。

## 四、典型工作流

**信号链验证**（SDG 输出 → SDS 测量）：
1. `sdg_set_wave(ch=2, wvtp="SINE", freq_hz=1000, amp_v=2)` 
2. `sdg_output(ch=2, on=True, expect_load="HZ", confirm=True)`
3. `sds_auto_scale(ch=4)`（信号接在 C4）
4. `sds_measure(item="PKPK")` + `sds_measure(item="FREQ")` 断言
5. 结束 `sdg_output(ch=2, on=False, expect_load="HZ", confirm=True)`

**示波器无波形**：
1. `sds_diagnose` 看触发源/电平/模式
2. `sds_auto_scale` 修正
3. `sds_screenshot` → **Read 返回的 PNG 看图**确认（测量值超屏被钳制不可信）

### 四.六、现成脚本：AI 自己做自动化测试/集成（**优先用这些，别临时现写**）

仓库里已有可直接跑的脚本（全部 ASCII 输出、证据自动落到 `TEST_DATA/**/*.json`）：

| 脚本 | 用途 | 碰设备? |
|---|---|---|
| `python -m keysight_3458a.preflight [--id\|--volts N\|--unstuck\|--watchdog N]` | 3458A **分层诊断**（默认只读）/ 最小电压测试 / 救砖 | 默认只读 |
| `python TEST_SCRIPTS/ks3458a/verify_3458a_offline.py` | 3458A 离线套件（74 项，含故障注入） | ❌ |
| `python TEST_SCRIPTS/ks3458a/verify_mcp_ks3458a_tools.py` | MCP 工具面/参数契约（14 项） | ❌ |
| `python TEST_SCRIPTS/ks3458a/verify_worker_isolation.py` | worker 隔离：硬截止 kill / 句柄回收 / 自动重启（7 项） | ❌ |
| `python TEST_SCRIPTS/ks3458a/verify_3458a_live.py` | 3458A **真机只读**冒烟（9 项：ID/状态/读数/统计） | ✅ 只读 |
| `python TEST_SCRIPTS/ks3458a/verify_3458a_live_extended.py` | 真机扩展（13 项：档位/自动挡/DINT 突发/ACV/恢复） | ✅ **改配置** |
| `python TEST_SCRIPTS/ks3458a/example_script_integration.py` | 脚本集成范例：`--dry-run`（不连设备）/ 库直连 / `--via mcp` | 视参数 |
| `python TEST_SCRIPTS/common/verify_mcp_tools_meta.py` 等 | 工具面/解析层/锁语义回归（不碰设备） | ❌ |

**建议顺序**（AI 自测或改完代码后）：离线 → 工具面 → worker → `preflight --id` → 真机只读 →
（确实需要时）扩展套件。每步失败就停，**不要**用 `reset`/`burst` 去"试出来"。

**外部项目集成范式（真实下游用户）**：SuperResistanceBridge（SRB V0.3，AD7190/LHA9954 电压 INL）
把 3458A 当**源的实测真值**（逐点配对 `ADC码值` + `V_3458A`，全程固定 NPLC，扫描只用单次读数），
自检顺序 = `PATH → connect(recover=True) → ID 校验 → ERRSTR → 配置 → 与另一台表同点对读`，
任一步失败即中止。详见 `docs/3458a_external_usage_srb_20260923.md`（含他们踩过的 3 个坑与我们
现在的对应状态：PATH 注入已由本库内部完成、burst 卡死已三层加固、RSRC_NFOUND/TMO 判据已进 preflight）。

**截图能力（2026-09-09 修复 alpha 后）**：
- `sds_screenshot` 返回 PNG 路径，**直接 Read 即可看图**（视觉判断最直观）
- 看得到：波形形态/有无信号/削顶/居中/面板菜单/光标读数/底部测量栏
- 与 SCPI 配合：截图看形态，SCPI 读精确数值
- 无视觉能力时用库 `analyze_screen()` 像素分析兜底（轨迹 Y 分布/削顶判定）

## 四.五、USB-TMC 卡死（故障兜底 `usb_reset`）

`*IDN?` 超时 / `VI_ERROR_TMO` / `VI_ERROR_SYSTEM_ERROR` 时：**先重连一次**（多数即恢复）；
仍不行 → `usb_reset(kind="dg", confirm=True, verify_idn=True)`（**重启该仪器的 USB
PnP 设备节点**：USB 重新枚举，**固件不重启、通道设定/输出/保护全部保留**，实测 2.4s）。
⚠ 该操作需**管理员权限**，而**MCP 进程不能自行提权、不会弹 UAC**：本机 zcode 里的
MCP 进程通常已带管理员（可直接用）；若工具报"需要管理员权限"，退出 MCP 走 CLI 提权：
`python common/usb_reset.py --kind dg --allow-reset --escalate --verify-idn`（弹 UAC）。
**不要**让人去拔插 USB，更不要给仪器断电（那是最后手段）。LAN 设备卡死不属此场景
（重连或换协议 inst0 ↔ raw socket）。

## 五、错误处理

### 读数/回读解读纪律（跨设备通用，AGENTS.md 铁律的速查版）

- **回读比对用容差，禁止 `==`**：仪器会舍入参数（实测 `12.1` 设进去回读 `12.099998`）。
  拿回读值做断言/比较一律带容差（相对 1e-3 量级起步）。
- **耦合参数同一消息连续发**：SCPI 里分开发的耦合设置会产生非预期中间态。
  走库工具（`sdg_set_wave` 等）已内部处理；**自己拼 `instr_write` 时必须注意**
  （如 SDS 的 scale+offset——改 scale 会等比缩放 offset，顺序错了值就错）。
- **自定义写序列前先 drain 错误队列**：`SYST:ERR?` 是 FIFO，上次会话残留的滞后
  错误会污染逐命令查错（写前 drain、写后逐条查——`instr_write` 已自动做）。
- **USB 设备一律走 VISA**：不要 pyusb/libusb 直连（Windows 无驱动时报
  NotImplementedError）；卡死恢复顺序见下方「USB-TMC 卡死」。
- **响应形态三种坑**：① 查询回**短格式**（设 `CHANnel1` 回 `CHAN1`）——解析要兼容；
  ② 布尔查询回 **0/1** 不是 ON/OFF；③ 带单位/回显头（SDS `C1:VDIV 5.00E+00V`、
  `2.00E-03S`）——剥干净再转数值。

### 设备特有坑（速查表之外的补充）

- **SDS**：`MEASure:MODE` 默认 ADVANCED——SIMPLE 组测量整体失效（`VALue?` 报
  "number of measurements is zero"）要先切 `MODE SIMPle`（`sds_measure` 工具已内置）；
  命令只吃**短形式**（`ACQ:MDEP?` 通、`:ACQuire:MDEPth?` 超时）。
- **RIGOL 示波器（DHO/MHO 共享内核 rigol_scope）**：系列差异一律走
  `rigol_scope/families.py` 的 Family 表（清测量 DHO `:MEASure:CLEar` vs MHO
  `:MEASure:DELete`；采集第四态 DHO `ULTRa` vs MHO `HRESolution`；边沿第三态
  **两系列都是 `RFALl`**）。**`*OPT?` 两系列都不要用**（MHO 实测查询超时）；
  MHO 的 ASCII 波形**不带 TMC 头**（二进制才带）。

统一返回 `{ok, error_type, error}`：
- `confirm_required`：补 confirm=True 重试
- `forbidden`：复位/存储覆写类，不可重试（不经 MCP，走测试脚本+显式授权）
- `param_validation`：改参数（枚举/范围错、查询缺 `?`）
- `connection`：设备离线（instr_discover 确认）
- `device_error`：设备拒绝/测量超时（读 error 文本，多为信号/触发问题）。
  **示波器"无有效值"会额外带诊断字段**（`mho_measure_item`/`dho_measure_item`）：
  `suspicious` = `channel_off`（通道显示关）/ `off_screen`（迹线在窗口外，附当前窗口与建议）/
  `near_edge`（极值贴窗口边沿，**可能是削顶后的假值**，必须换档；附 `edges_touching`/`edge_hints` **分顶/底**）/ `few_edges`（屏内不足
  2 个周期，附时基建议）/ `no_signal`；另有 `hint`、`window`、`evidence`（逐条原始响应）。
  先看 `suspicious` 再决定下一步，不要一律"检查信号接入"。
- `communication`：IO 异常（重试一次，仍失败检查连接）
- `timeout`：调用超墙钟上限（默认 150s，可用 `INSTRUMENT_CALL_BUDGET_S` 调）——
  设备离线/总线挂起；**命令可能已下发**（写操作请回读确认）
- `device_busy`：**设备正被另一个调用占用**（本次未下发任何命令、也未排队）。
  通常是上一个调用很慢或已超时但 worker 仍在跑——等它结束后重试即可；只有在长时间
  持续 BUSY（数十秒到数分钟以上）时才说明底层驱动真卡死，此时用
  `run_remote.ps1 restart` 恢复（见下方「MCP 服务不可用」）

### MCP 服务不可用：Agent **自己拉起**，不要问用户（2026-10-08 起）

`instruments` 是**常驻单例**（streamable-http，`127.0.0.1:8765/mcp`），由"登录时启动"的
计划任务拉起 —— 但任务**只在登录时跑一次**，服务中途挂掉不会自愈。所以由 Agent 兜底，
**不要为此打断用户**。

**先分辨是哪一类故障**（别把设备问题当服务问题）：

| 现象 | 含义 | 处置 |
|---|---|---|
| **所有** `instruments_*` 工具都失败 / 客户端报连不上 | 服务挂了 | **自己拉起**（下面命令） |
| 只有某个工具报 `connection` | 服务在跑，是**设备**离线 | 按本节其余流程排查，**不要重启服务** |
| 报 `timeout` / `device_busy` | worker 卡在驱动里 | 先等它结束；持续数十秒以上才 `restart` |

**自救命令（幂等，随时可跑，安全）**：

```powershell
pwsh -NoProfile -File <仓库根>\mcp_instruments\run_remote.ps1 status
# running:false → 拉起；running:true → 服务没挂，去查别的原因
pwsh -NoProfile -File <仓库根>\mcp_instruments\run_remote.ps1 start
```

`start` **幂等**（已在跑就 no-op），且用 **detached 进程**启动 —— 不依赖这个 shell 存活，
拉起后**重试原来的工具调用**即可。若 `start` 自己失败（会打印 stderr 尾部），
把原文报给用户，**不要反复重试**。

`stop` / `restart` 会中断**其他客户端**正在跑的仪器操作 —— 只在确认服务真的卡死时才用。

### 卡死后的恢复：为什么"重启 MCP"确实有效（2026-09-16 实测）

执行器的 worker 是 **daemon 线程**（曾用 `concurrent.futures.ThreadPoolExecutor`，
但它的 `atexit` 会 join 非 daemon worker——驱动挂起时**整个进程退不出来**，
"重启 MCP"这条恢复路径反而失效；离线实验与回归见
`TEST_SCRIPTS/common/verify_executor_exit.py`）。所以现在：即使某次调用卡在驱动里，
进程仍能正常退出/被杀，重启即可恢复。

重启方式（2026-10-08 单例化后）：`pwsh -File mcp_instruments\run_remote.ps1 restart`。

### ⚠ 同一台仪器**绝对不能**两个会话并发（2026-09-17 真机实测）

不是"会互相干扰"，而是**会互相读到对方的响应**：DG832(USB-TMC) 两会话并发实测约一半查询
丢响应 + 100+ 次答非所问（问 `:SOUR1:FREQ?` 收到 `*IDN?` 的回答）；MHO 的 VXI-11/raw
两会话同样串台。**收到的都是合法响应**，靠"成功/失败"判断不出来；并发超时未读还可能把
设备侧响应流搞坏（MHO 的 VXI-11 通道会稳定滞后一条，且**不解自愈**）。串行化后同一负载
0 串台 0 错误——这正是本服务器"单进程单 worker"的原因。证据见 `docs/visa_concurrency_20260917.md`。

- **纪律**：同一台仪器同一时间只由一个客户端操作；换客户端前先收尾。
- 若怀疑通道错位：工具会报"**地址校验失败**（`*IDN?` 不像该设备）"——这是护栏在挡，
  不是地址被 DHCP 抢了；处置：换协议（VXI-11 ↔ raw）或重置设备 LAN/重启。

### 跨进程会话锁（自动告警）

每次设备工具调用都会刷新本进程的会话锁（`common/session_lock.py`）：

- 若**另一个进程**正在用**同一地址**：返回体多一条 `warnings`，内容含对方 PID 与
  "同一设备两会话并发会响应串台"的实测依据 —— **只告警不阻塞**，请自行决定是否等它结束；
- 若对方用的是**同一台仪器的另一接口**（如 inst0 vs 5555）：另给一条
  "**跨接口并发未验证**"的告警（不参与判重）；
- 见到 `warnings` 里出现"响应错位"字样（如 `RigolScope._float()` 报
  "响应错位：:ACQuire:SRATe? 回 'NORM'"）→ 说明仪器响应流已被并发/超时未读污染，
  **数据不可信**：先换协议（VXI-11 ↔ raw）或重置该仪器 LAN/重启，再继续测。

### ⚠ 串行化的边界：进程内全局串行，跨进程仍不互斥（2026-10-08 单例化后）

设备锁（`_DEVICE_LOCK`）与单 worker 执行器都是**进程内**的 —— 本进程内是**全局串行**
（不只是同一台仪器，是所有设备操作）。2026-10-08 起 `instruments` 默认以**常驻单例**
运行，正常情况下全机只有**一个** MCP 进程，同一仪器不会并发。

- 仍要警惕**多实例**：外部脚本、未切 remote 的旧客户端（DSH / zcode）、或手工起的
  stdio 实例都各自成进程，**进程之间不互斥**，可同时操作同一仪器（响应串台）。
- 排查（PowerShell）——看到**多于一个** `server.py` 实例就要查：
  `Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -match 'mcp_instruments' } | Select ProcessId,ParentProcessId,CreationDate`
  多于一个通常说明有客户端没连单例（检查它的 MCP 配置是否 remote）。
- 纪律：**同一台仪器同一时间只由一个客户端操作**；交接前先收尾（关输出/恢复设定/
  归还面板控制权）。

### 超时/卡死语义（所有工具一致）

自 2026-09-15 起，所有工具经**统一异步调用边界**执行（单 worker 执行器；设计评审见
`docs/gpt_qa/20260915-mcp-async-refactor.md`）：

- **一次只跑一个设备操作**（VISA 必须串行）。设备忙时新调用**立即**返回
  `device_busy`（不排队、不白等、不下发任何命令），文案里说明「已忙多久」；
- **等待上限（deadline）**：默认 150s；`sds_auto_scale` 实测最坏 ~90s，故单独放宽到
  300s；通用工具随其 `timeout_ms` 推算（下限 30s）。超时返回 `timeout`。
  可用 `INSTRUMENT_CALL_BUDGET_S` 覆盖默认值；
- ⚠ **超时只代表「调用方不再等待」，不代表操作已停止**：worker 会继续跑完，设备保持
  BUSY 直到它真正结束（随后自动恢复，**不需要重启**）。**有副作用的命令超时后请回读
  确认**——它可能已经生效；
- 事件循环不再被阻塞：慢调用进行中，协议层（`tools/list`、取消等）仍能即时响应——
  这是本次改造的核心收益（旧版同步直跑事件循环，会整个冻住）；
- 串口探测在**子进程**里做：某些串口会让驱动层 open 永久挂起，在本进程里探测会留下
  卡死线程、导致**之后任何 VISA 调用都把服务器进程打死**（2026-09-15 实测）。子进程
  超时可杀，卡住的句柄随之消失。

> 局限（如实记录）：worker 无法被安全强杀，极端情况下仍可能长期 BUSY（底层驱动彻底
> 卡死）。要「到点无条件恢复」需把 VISA 挪进独立子进程再 kill——属独立课题。
