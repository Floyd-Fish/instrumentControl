# instrumentControl — Agent 工作规范

仪器控制集合项目：VISA/SCPI 统一发现层（common/）+ 按设备分库
（dh1766_control / dho_control / mho_control / sds_control / sdg_control /
keysight_3446x / dg832_control / emoe_control）+ MCP 统一暴露（mcp_instruments/）。
AI/Agent 操作仪器必须遵守以下规范。

## 一、SCPI 客户端铁律（实测教训，违反必踩坑）

1. **命令禁止猜测**：落码前必须对照设备手册（各库 docs/ 下有提取版）核对语法。
   历史事故：`:CONF VOLT:DC`（应为 `:CONF:VOLT:DC`）积压 12 条错误；
   `:SYST:FACT`（不存在）已删除；SDS `ITEM` 漏 `,ON` 参数。
2. **写序列前先 drain 错误队列**：错误队列 FIFO 滞后报错会污染逐命令查错
   （`sds_control.sds.drain_errors`）。写后逐条查 `SYST:ERR?`。
3. **写操作三步**：写入 → 查 SYST:ERR? → 回读比对。缺一不可。
4. **查询无副作用**（SCPI-99 §6.2.3）：例外仅 MEASurement 子系统。
5. **字符查询返回短格式**（§6.2.3）：设 `CHANnel1` 查回 `CHAN1`；解析响应要兼容。
6. **布尔查询返回 0/1**（§7.3），不是 ON/OFF。
7. **设值后必须回读验证**（§7.2）：仪器可舍入参数（如 12.1 → 12.099998，
   比较必须用容差，禁止 `==`）。
8. **耦合参数同一消息连续发送**（§编程提示#3），分开发送产生非预期中间态。
9. **读数精度看格式：`BYTE` 波形是 8bit，只用于看形态；量值一律读 `WORD`。**
   实测（MHO984D，2V/div）：`BYTE` 的 `YINCrement` 是 `WORD` 的 **256 倍**
   （0.0683 V/code vs 0.000267 V/code），0.25V 的小信号只占 **3 个码值**——
   此时 ±1 码量化误差就是 ±30%，拿 BYTE 读数去比设备测量值会得到"差 20%"的假告警。
   （真机留痕：`TEST_DATA/mho/verify_mho_*.json` 的"冻结态 WORD/ASCii 一致 <2%"与
   "BYTE 按量化容差"两条断言。）

16. **响应形态因厂而异**，客户端必须兼容：
   - 回显头：SDS 查询响应带命令头（`C1:VDIV 5.00E+00V`）
   - 单位后缀：`2.00E-03S` / `5.00E+00V` / `VDC`
   - 短格式：`CHAN1` / `NORM` / `SINC`
   - 钳制假值：**示波器信号超屏时 MAX/MIN/PKPK 读数被钳制在屏界，不可信**——
     削顶/居中判断用截图像素（`sds_control` analyze_screen），不用设备测量
10. **命令形式因厂而异**：SDS 只吃短形式（`ACQ:MDEP?` 通、`:ACQuire:MDEPth?` 超时）；
    Keysight 冒号嵌套（`:CONF:VOLT:DC`）；Siglent SDG 参数键值对（`C1:BSWV WVTP,SINE`）。
11. **测量模式开关**：SDS `MEASure:MODE` 默认 ADVANCED，SIMPLE 组整体失效
    （VALue? 返回 'The number of measurements is zero'）——先切 `MODE SIMPle`。
12. **触发模式 AUTO 防冻结**：NOISE 等无规则波形在 NORMal 下永不触发 → 采集冻结 →
    后续测量全 `****`。`sds_control.auto_scale` 自动切 `:TRIGger:MODE AUTO`。
13. **同厂商不同系列命令集不同**：DHO 的 `:ACQuire:TYPE` 无 HRESolution（SDS 才有）；
    未开启的 RIGOL 通道写 SCALe 被拒（-200），需先 `:CHANnel<n>:DISPlay ON`。
    **DHO800/900 与 MHO900 命令集 97% 重合、已合并到共享内核 `rigol_scope/`**
    （2026-09-15，证据 `docs/rigol_scope_compare_20260915.md`）。真差异只有少数几条，
    一律走 `rigol_scope/families.py` 的 Family 表：清测量 DHO `:MEASure:CLEar` /
    MHO `:MEASure:DELete`；采集第四态 DHO `ULTRa` / MHO `HRESolution`；
    `:ACQuire:BITS` 与 `:CHANnel<n>:Impedance` **仅 MHO**；`*OPT?` 两者都不要用
    （MHO 实测查询超时）。边沿第三态**两系列都是 `RFALl`**——`RFail` 曾是
    `dho.py` 的 docstring 笔误，已被文档互相抄写固化，2026-09-15 按手册原文更正。
    MHO 波形 ASCII 格式**不带 TMC 头**（二进制才带；DHO 未实测——**待办**：
    DHO 真机复验交接单 `docs/dho_live_verification_handoff.md`，6 项检查 + 不符时改哪个字段）。
14. **USB TMC 一律走 VISA**：禁止 pyusb/libusb 直连（Windows 无驱动时
    NotImplementedError）。LAN raw socket 会话必须配 `\n` 终止符。
    **USB-TMC 卡死的恢复顺序**（真机实测 2026-09-15）：
    ① `*IDN?` 超时 / `VI_ERROR_TMO` / `VI_ERROR_SYSTEM_ERROR` 先**重连一次**
       ——多数情况重连即恢复（DG832 实测遇过一次 `VI_ERROR_SYSTEM_ERROR`，重连后正常）；
    ② 重连仍不行 → **重启该 USB 的 PnP 设备**（不是给仪器上下电）：
       `python common/usb_reset.py --kind dg --allow-reset --verify-idn`
       ——实测 **2.4 秒**恢复，**仪器固件不重启、通道设定/输出/保护 100% 保留**；
    ③ 该操作改设备节点需要**管理员权限**（会弹 UAC）：管理员进程可直接跑，否则加
       `--escalate` 自动弹 UAC；先 `--dry-run` 可只打印将要执行的
       `pnputil /restart-device` 命令（不需权限）；
    ④ 还不行才拔插 USB / 换口 / 仪器断电（最后手段）。
15. **示波器"无波形/测量全 `****`"标准排查流程**（先读后写，截图辅助）：
    a. **先读配置不猜**：触发源/触发电平/触发模式/通道开关/时基/垂直档位/采集参数
       ——`sds_control.diagnose_trigger()`、DHO 用 `snapshot()`；
    b. 常见坑：触发源挂空通道（实测：源=C1 电平 12.2V 而信号在 C4）、
       NORMal 模式遇 NOISE 类无规则信号（永不触发→采集冻结）、通道未开启、
       档位与信号量级不匹配（超屏读数被钳制）；
    c. **修正顺序**：触发源→目标通道 → 模式 AUTO → 电平归信号中点 →
       通道开启 → auto_scale 自动定标；
    d. **截图辅助**：`screenshot_png()` 产出的 PNG **可直接读图**（AI 视觉判断）
       ——2026-09-09 修 BMP alpha=0 致全透明问题（存前 convert('RGB')）；
       无视觉能力时用 `analyze_screen()` 像素分析兜底。两者都是削顶/居中/
       有无波形的物理真相，设备测量值超屏被钳制不可信；
    e. **auto_scale 两条路径**（默认 SCPI 闭环只动目标通道，多信号安全；
       `:AUToset` 是全局破坏性命令会重置所有通道——仅在确认"简单周期信号+
       无其他已调好通道"时才 `use_autoset=True` 显式启用）。
16. **示波器垂直档位/偏置的三条设备行为**（MHO984D 现场实测 2026-09-15，
    证据 `docs/tool_optimization_20260915.md`；已固化成
    `rigol_scope` 的 `configure_channel/fit_channel` 语义 + 离线回归
    `TEST_SCRIPTS/common/verify_rigol_scope_semantics.py`）：
    a. **屏幕中心电压 = −offset**（不是 +offset；按 +offset 理解会把波形顶出屏幕，
       读数变成垃圾值）；窗口 = `[−offset−4·scale, −offset+4·scale]`（MHO 实测 8 格）；
    b. **改 `SCALe` 会等比缩放 `offset`**（设备主动改写以保持波形屏幕位置）→
       任何设置序列**必须先 scale 后 offset**，且**两者一起回读**（只回读被写的那个
       等于没回读）；
    c. **通道 OFF 时写 `SCALe`/`OFFSet` 被静默忽略**（`syst_errors=[]` 无任何错误码）→
       要设参数先开通道，设完再关；
    d. 偏置有**量程上限且随档位变**（2026-09-16 实机阶梯：0.05 V/div→±1 V、
       0.1~0.2→±10 V、0.5~2→±20 V、5~10→±100 V；此前"±20 V 与档位无关"只在
       0.5/2 两个档位上测过，结论不完整）→ 小档位下"居中"可能做不到，要先抬档位；
       写后必须比对回读，"设备没照做"要如实报（`adjusted`+`reasons`），不许混成成功；
    e. 部分削顶时测量值可能是**"看着合理的假值"**（曾读到 0.9216 V 而真实 ±10 V）→
       极值贴窗口上下沿即不可信；无有效值（`9.9E37`）时按
       `suspicious` 分类（离屏 / 边沿不足 / 通道关 / 无信号），不要只说"检查信号"。

17. **VISA 并发：不同设备可以并发，同一台设备**绝对**不行**（2026-09-17 两台真机实测，
    证据 `docs/visa_concurrency_20260917.md` + `TEST_SCRIPTS/common/verify_visa_concurrency.py`）：
    a. **共享一个 ResourceManager + 多线程访问不同设备 → 安全**（300 次并发查询 0 错误，
       真并行；`identify_lan_all` 的 32 并发扫不同主机属这一类）；
    b. **同一台设备开两个会话并发 → 静默串台**：USB-TMC（DG832）实测约 **一半查询丢响应**
       + 100+ 次"答非所问"（A 问 `:SOUR1:FREQ?` 收到 B 的 `*IDN?` 回答）；MHO 的
       VXI-11/raw 两会话同样串台。**值本身都合法**，用"成功/失败"判断不出来；
    c. 串行化（一把锁）后同一负载 **0 串台 / 0 错误** —— 这就是"单进程单 worker"存在的理由；
    d. 并发超时未读还会**搞坏设备侧响应流**：MHO 的 VXI-11 通道被搞成稳定滞后一条，
       **新进程/新会话/`clear()`/`*CLS` 都解不开**，只能换协议（VXI-11 ↔ raw，实测 raw 对齐）
       或设备侧重置 LAN/重启；USB 关掉会话即可恢复；
    e. **一条客户端纪律**：同一台仪器同一时间只由一个客户端/一个会话操作——MCP 的
       `_verify_idn` 会把错位通道挡在门外（报"地址校验失败"），但**跨进程**不互斥，
       多实例并发仍是本仓已知风险；
    f. 同一资源并发**会话数**也有上限（实测 20 线程并发 open 只有 15~16 成功，
       其余 `VI_ERROR_ALLOC`）——别对一台设备开一堆会话。
    g. **跨进程咨询锁**（`common/session_lock.py`，每次设备工具调用自动生效）：
       判重**按 VISA 地址**，同地址的活跃进程会让工具返回体多一条 `warnings`
       （只告警不阻塞）；同一台设备的**另一接口**（inst0/5555/HiSLIP）另给一条
       "跨接口并发未验证"的告警。**并发风暴会污染仪器响应流**（实测：MHO 的
       VXI-11 与 raw 都被打成持续错位，需面板重置 LAN）——诊断见
       `RigolScope._float()` / `align_session()`（把"莫名解析错"变成"响应错位 + 处置建议"）。
18. **raw socket（RIGOL 5555 等）没有消息分帧**：大二进制块必须**按 TMC 头长度循环读满**
    （`common/visa_client.py::VisaClient.query_block`；`screenshot()` 已改用它）。
    实测（2026-09-17）：MHO 整屏 PNG 97 471 字节，raw 上单次 `read_raw()` 只拿到 **6 字节**
    （VXI-11 因有分帧才一直没暴露）；小窗读（`get_waveform` 分片）不受影响。

## 二、安全红线

- **电源操作先查输出模式**（`psu_mode()`/`output_mode()`）：DH1766 的 CH1/CH2 有
  正常/串联/并联/跟踪四态（手册 §3.8）。跟踪(TRAC)下 CH2 跟随 CH1 输出同等值
  **负电压**——看到 CH2 负压不要当成故障或"固定负轨"。切换模式前输出必须全关
  （继电器联动，库内无条件强制）；三模式固件互斥（开一路自动清零其余）。
- **电源输出开关必须声明当前模式**（`set_output(ch, state, expect_mode)` /
  `set_output_all(states, expect_mode)` / MCP `psu_output(..., expect_mode)`）：
  仅校验不设置，与实际不符立即拒绝并回传当前模式——防止在不知拓扑
  （TRAC 联动/SERI/PARA 合并）时误操作输出。
- **信号源输出开关必须声明负载**（`set_output(ch, on, expect_load)` /
  MCP `sdg_output(..., expect_load)`）：HZ=高阻（AMP 即 Vpp）/ 50=50Ω
  （实际幅度减半），仅校验不设置，不符立即拒绝并回传实际值。
- **禁止远程锁定命令**：`SYSTem:REMote ON`（SDS：禁用触摸屏/面板按键，界面显示 Remote）、
  DH1766 的 `SYST:RWL`（面板 Lock 键不可切回本地，需 `SYST:LOC` 恢复）及
  `SYST:REM`/`SYST:LOCK`/`:SYST:COMM:RLST <state>` 类——妨碍现场人工操作。
  MCP `instr_write` 黑名单已全拦（2026-09-13 补 `SYST:RWL` / `:SYST:COMM:RLST` 缺口；
  2026-09-15 补 `instr_query` / `readback_cmd` 两条走私通道与 `SYST:RESE` 类**中间缩写**，
  并把 MHO 的 `:SYSTem:LOCKed`（屏幕/键盘锁定）纳入同族）；
  **纯查询形式放行**（`SYST:REM?`/`SYST:COMM:RLST?`）——用于诊断面板是否被锁，
  且纯查询不改变锁定状态。**查询口判据＝逐段判"每段都是查询单元"**（命令头以 `?` 结尾、问号后可带参数）：
  **多段纯查询放行**（`:CHANnel4:DISPlay?;:CHANnel4:SCALe?` 这类多段回读是合法常用写法），
  但只要有一段是写命令/复位/锁定就整条拒（`:CHANnel4:DISPlay?;:OUTP4 ON` 这种"查询后夹写"
  是本仓库明确要拦的走私形态）。依据：SCPI 里 `;` 分隔的是**同一条消息内的多个命令单元**，
  设备会逐个执行（Keysight 手册明文；DG832 实测 `:SOUR1:PHAS?;:SOUR1:PHAS 123` 的写单元真的
  生效，证据 `TEST_DATA/dg832/semicolon_units_probe_20260915.json`）。
  **写路径**（`instr_write`）同样**逐单元**查黑名单——写命令天然会用 `;` 串联。
- **DH1766"一连就进远程模式"是设备行为**（2026-09-13 实测，V0.1.4.3）：
  任何远程会话都会把电源置为 `REM`——新建会话第一条命令查 `SYST:COMM:RLST?`
  即返回 `REM`；发 `SYST:LOC` 立即回到 `LOC`（只交还面板控制权，
  **不影响输出/电压/模式**）。注意手册写的 `SYST:COMM:RLST:STAT?` 在本机
  **无响应**（不是此前记的"返回空串"）。MCP 电源工具已在会话收尾自动补发
  `SYST:LOC`（`server.py::_psu_close`），使现场面板随时可用。
- **禁止复位类命令**：`*RST`、`:SYST:RESet`、`:SYST:FACT`、DMM `*RCL/*SAV` 覆写。
  `*RST` 需用户显式授权（dh1766 用 `--allow-rst` 模式）。

  **两条"复位"语义不同，别搞混**（2026-09-15 按手册原文核对，RIGOL DHO/MHO 两系列一致）：
  `:SYSTem:RESet` = 「使系统重新上电」→ **重启**（DHO 3.24.11 / MHO 3.24.12）；
  `*RST` = 「将仪器恢复至出厂默认状态」→ **恢复出厂**（两系列 3.12.2）。
  旧 `dho.py` 把 `:SYSTem:RESet` 注释成"恢复出厂默认"，已按手册更正。

  **处理方式（"保留但绝不顺手可用"）**：命令常量保留在各库 `commands.py`
  （`SYST_RESET` / `RST`，附逐条语义与风险说明，审计器也据此核对出处）；
  **库内不提供任何公开方法、MCP 不暴露工具**；万一要用走受控脚本
  `TEST_SCRIPTS/common/rigol_scope_reset.py`（`--info` 只打印说明、不碰设备；
  真发命令需 `--allow-reset` + 显式选 `--reboot`/`--factory`，并留痕）。
- **输出/信号类操作**（SDG 输出开关、电源输出开关）需明确场景授权：
  MCP 的 `sdg_output`/`psu_output` **开与关都必须 confirm=True**——关闭同样
  可能打断正在进行的测试或他人实验（配合 expect_load/expect_mode 状态校验）。
  **关断的 confirm=True 代表已获授权**，仅两种情况可填：① 用户本轮明确要求关闭，或明确要求做上下电/上下电循环；
  ② 用户明确声明独占使用（"只有你在用这块板子"）。否则先向用户确认再执行。
- **测试脚本必须 try/finally 恢复被改设定并关闭输出**（备份→改→回读→恢复）。
- **留痕**：实测输出 JSON/CSV 到 `TEST_DATA/<device>/`，时间戳命名防覆盖。
- **测试脚本**放 `TEST_SCRIPTS/<device>/`；手册提取放各库 `docs/`。

## 三、常用入口

- 统一发现：`common.find_device(idn_contains, resource, hosts, allow_scan, cidr)`
  查找链：显式 resource → hosts(自动选协议 inst0/hislip0/raw5025/raw5555) →
  VISA 列表 → CIDR 扫描（默认关，最后手段）
- 地址解析（**禁止写死 IP**）：`common.resolve(kind, resource=None)`，
  kind ∈ sds/sdg/dmm/dho/psu；MCP 专用工具与 `TEST_SCRIPTS/` 共用它
  （实现在 `common/resolver.py`）
- 冒烟脚本：`TEST_SCRIPTS/common/test_discovery.py`(T1~T5)、
  `three_libs_smoke.py`、`libs_full_verify.py`、`waveform_matrix.py`(SDG→SDS 闭环)
- **五台设备全链路只读验收**：`TEST_SCRIPTS/common/verify_all_devices.py`
  （发现→解析→身份校验→快照/测量，全程零状态变更；真机跑一遍约 1 分钟，留痕到
  `TEST_DATA/common/verify_all_devices_*.json`）
- 命令审计器：`TEST_SCRIPTS/common/audit_all_commands.py`（新增命令后必跑，
  防猜测命令回归；判据=段键元组+段内长短形式兼容+后缀路径命中，见文件头 docstring）
- **护栏覆盖性审计**：`TEST_SCRIPTS/common/audit_guardrail_coverage.py`（改护栏/白名单后必跑）
  —— 用手册全部命令穷举黑名单与查询判据（查误伤），并把各工具入参白名单与手册枚举**双向**比对
  （查"手册有我们缺"= 会误拦）。首轮抓到 3 个真缺陷，见 `docs/review_20260915.md` §四.五
- **发现层网络/分块验收（离线，只用 loopback）**：`TEST_SCRIPTS/common/verify_discovery_local.py`
  —— `local_cidrs()` 必须用接口**真实掩码**（本机仪器网是 `192.168.1.100/16`，
  按 /24 算会把同广播域但不在 /24 内的仪器**静默漏掉**）；`probe_open_ports` 分块防
  大网段 MemoryError；宽网段预筛走异步分块（/16 ≈166s，线程版要 5 分钟）
- **`instr_discover` 的两处耗时修复（2026-09-23 实测 190.2s → 20.6s，-89%）**：
  ① **网段分层 + 命中即停**：窄段（/24 及更窄）先合并扫，**命中任何仪器就停**；只有
     窄段一无所获才扫宽网段，且跳过已扫过的地址。根因是原实现为省"逐段串行"的调度
     开销把所有网段**合并**成一次预筛，于是把本机 ZeroTier 的 `172.29.0.0/16`
     （65534 个地址、预筛 **168.0s**、存活 0）也吞了进来——而三个 /24 合计仅 **2.0s**，
     仪器（SDS/SDG/DH1766）全在 `192.168.31.0/24`。
     ⚠ **不要改成"跳过虚拟网卡网段"**：本机 `10.165.75.0/24` 同样是 ZeroTier、却有设备
     响应，远程实验台也完全可能挂在 VPN 网段里。宁可多扫一轮，不可静默漏检
     （与"真实掩码"那条同一个教训）。
  ② **串口超时收紧**：读超时 `1500 → 500ms`、子进程硬超时 `8.0 → 2.0s`、
     `open_timeout 2000 → 1000ms`。依据：`*IDN?` 响应最长约 100 字节，按**最慢
     4800 baud** 也只有 100×10/4800 ≈ 0.21s；正常口实测 **0.44s** 就完成，余量充足。
     挂起口是靠子进程 kill 收场的，旧值 8s 并不让它变成成功，只让每个挂起口白等 8s
     （本机 5 个挂起口 × 8s ÷ 4 workers ≈ 10s）。
     实测依据留痕：`TEST_DATA/common/profile_discovery_20260923_175900.txt`。
- **MCP 工具元信息验收（离线，只握手不碰仪器）**：`TEST_SCRIPTS/common/verify_mcp_tools_meta.py`
  —— 断言每个工具的 description 非空（取自 docstring）、inputSchema 完整、关键工具在列、
  stdout 无 print 污染；**新增工具后必跑**（漏写 docstring 会在客户端显示成"无描述"）
- **审计器自测（离线闭环）**：`TEST_SCRIPTS/common/verify_audit_extractor.py`
  —— 提取/归一化/全仓 MISS 基线三层断言，改审计器后必跑（无仪器也能跑）
- 审计报告：`docs/command_audit_full_20260823.md`（脚本自动生成，重跑即覆盖；
  **MISS 需人工甄别**——历史甄别口径见 `docs/command_audit_20260823.md`）
- **DH1766 专项审计**：`docs/dh1766_audit_20260915.md`（safe_mode 旁路等 9 项，
  含离线补丁与"仪器回来后"的真机回归清单）
- 操作手册：`docs/AI_OPERATION_GUIDE.md`（API/固件特性/闭环范例）
- **实测记录**：`docs/TEST_RECORDS.md`（历轮实测时间线；README 只放项目定位与用法）
- 设备经验：`dh1766_control/docs/EXPERIENCE.md`（时序/固件差异/上电过渡态）
- MCP 服务器：`mcp_instruments/server.py`（68 工具 = 64 设备专用 + 3 通用护栏 + 1 故障兜底
  instr_discover/instr_query/instr_write/usb_reset——新设备零代码接入；zcode 用户级 config 已注册
  `instruments`；工具选择/参数语义/安全门见 skill `instrument-mcp`）
- **MCP 单例运行（2026-10-08）**：`server.py` 支持 `MCP_TRANSPORT=streamable-http`
  常驻单例（`mcp_instruments/run_remote.ps1 start|stop|restart|status|install-task|uninstall-task`，
  默认 `http://127.0.0.1:8765/mcp`），供所有项目共用，取代"每个工作目录各起一个 stdio 子进程"
  （本机曾 12 份 × 805 MB ≈ 9.7 GB → 单份 68 MB）。**自启 = 仅登录触发的计划任务**
  `instrumentControl-MCP-Server`（`run_remote.ps1 install-task` 注册）。**不设该变量时行为与原来
  完全一致（stdio = 回滚路径）**；未知取值显式报错、不静默回退。运维口径对齐 kimi-webbridge：
  start 幂等、pid/addr 状态文件在 `mcp_instruments/run/`（gitignore）、绝不静默换端口、
  健康检查走真实 MCP `initialize` 握手。进程内 `_DEVICE_LOCK` + 单 worker 已全局串行，
  单例后同仪器不会并发（`session_lock` 的跨进程告警对 stdio 外部脚本仍有效）。
  **注意**：`opencode mcp list` 不支持 `--global`（只读项目级配置），验证以"重启后在会话里
  实际调用仪器工具"为准。**服务挂了由 Agent 自己拉起、不要问用户**：`run_remote.ps1 status`
  看 `running`，为 false 就 `start`（幂等 + detached 进程，不依赖 shell 存活），然后重试工具调用；
  详细判据与"别把设备问题当服务问题"的分辨表见 skill `instrument-mcp` →「MCP 服务不可用」。
- **skill 的权威源在仓库 `skills/<name>/SKILL.md`**（如 `skills/instrument-mcp/SKILL.md`）：
  **不要**在 `~/.agents/skills/` 下另存一份副本——那里是 `scripts/install_skills.py`
  建的分发链接（junction），改哪边都是同一份文件。安装/检查/卸载用该脚本
  （默认装到 `~/.agents/skills`；`--check` 只报告，`--copy` 复制，`--uninstall` 卸载）。

## 四、当前设备与地址解析

**地址不是固定资产，禁止写死**：仪器 IP 随 DHCP 续租/换网段变化，USB 换口换资源串，
串口 ASRL 编号漂移。因此本表只列"发现入口"，**不列地址**；任何文档/脚本/代码都不得
把某个具体地址当成设备资源（历史上曾把 2026-08 实测地址写进资源表，换网段后照抄即失败，
甚至可能连到同网段其他设备并对它下发 SCPI）。

> **本机具体设备身份（序列号/固件/实测资源串/现场带电状态）在
> `docs/DEVICE_FACTS.local.md`**——那是**本机专有事实**（换机器即失效，故不入库、
> 已 gitignore）。需要写 `*IDN?` 断言、核对该机固件行为、或排障查"这台设备是什么"
> 时看那份；**代码与脚本仍然一律走解析层，不得照抄其中的地址**。
> 换设备/换固件/换地址后请更新该文件。

| 设备 | 库 | 发现入口（库函数 · 解析层 · MCP 前缀） |
|---|---|---|
| DH1766A-1 电源 | dh1766_control | `find_dh1766()` · `resolve("psu")` · `psu_*` |
| RIGOL DHO924S 示波器 | dho_control | `find_dho()` · `resolve("dho")` · `dho_*` |
| RIGOL MHO984D 示波器 | mho_control | `find_mho()` · `resolve("mho")` · `mho_*` |
| RIGOL DG832 信号源 | dg832_control | `DG832()`（自动发现）· `resolve("dg")` · `dg_*` |
| Siglent SDS824X HD | sds_control | `find_sds()` · `resolve("sds")` · `sds_*` |
| Siglent SDG2122X 信号源 | sdg_control | `find_sdg()` · `resolve("sdg")` · `sdg_*` |
| Keysight 34465A 万用表 | keysight_3446x | `find_dmm()` · `resolve("dmm")` · `dmm_*` |
| HP/Keysight 3458A（八位半，**非 SCPI**） | keysight_3458a | `find_3458a()` · `resolve("ks3458a")` · `ks3458a_*` |
| Emoe 校准器（骨架） | emoe_control | `instr_discover`（仅发现 + *IDN?，编程手册未提供）。**ASRL 编号漂移最频繁**：校准器原 ASRL31 现离线、ASRL5 现为 ADS127L11-DAQ-EV——串口设备一律先重发现 |

**3458A 接通要点（2026-09-23 本机实测，82357B USB/GPIB）**：它不是 SCPI 表（无 `*IDN?`，
用 `ID?`；无 `SYST:ERR?`，用 `ERRSTR?`；复位是 `RESET`；读数是 `TARM SGL,1`），
**必须走 Keysight VISA 核心 `ktvisa32.dll`** 并先 `SetDllDirectoryW(<IO Libraries Suite>\bin)`
+ 预加载 `ioGPIB.dll`/`ioGpibIntfc.dll`——系统默认 `visa32.dll`（NI/IVI 壳）会报
`VI_ERROR_LIBRARY_NFOUND`（NI-488.2 / 32 位 Tulip 护照都不在本机）；SICL `iopen` 同因抛
`0xE06D7363`。另：若 `TRIG?`=4(HOLD)，`TARM SGL,1` 永不出数，须先 `TRIG AUTO`
（库已用 `prepare_for_read()` 处理，**不 RESET**）。命令白名单与实测响应见
`keysight_3458a/docs/COMMANDS_3458A.md`。

地址解析链（`common/resolver.py`，MCP 服务器与 `TEST_SCRIPTS/` 共用同一套来源）：
**显式入参 > 环境变量 `INSTRUMENT_<KIND>_RES` > 本机配置
`%LOCALAPPDATA%\instrumentControl\devices.json` > 上次成功缓存
`last_good_resources.json` > `find_device()` 自动发现**（默认不扫网段，
需要自动扫描设 `INSTRUMENT_ALLOW_SCAN=1`；LAN 未注册设备先跑 `instr_discover`，
其发现结果会按 `*IDN?` 自动回写缓存）。

- 本机默认地址用 `python mcp_instruments/config_cli.py show|init|set|autofill|clear` 维护
  （`set <kind> <resource|host>`；`devices.json` 本机专用、不入库，用户可手改）；
- **地址值一律是完整 VISA 资源串**（TCPIP/USB/ASRL/GPIB 同一形态），**不要自己拼
  `IP:端口`**——协议/端口/参数因设备而异；需要从裸 IP/host 起时交给
  `config_cli.py set <kind> <host>` 或 `resolve`/`canonicalize()`：先探测协议
  （inst0→hislip0→raw5025→raw5555）再核对 `*IDN?`，只把规范串落库；
- **连接后会核对 `*IDN?`**：地址若已被 DHCP 分配给别的设备，工具直接拒绝操作
  （报"地址校验失败…请先 instr_discover"），绝不把 SCPI 发给未知设备。

**动 DH1766 之前先 `psu.status` 查当前模式与带电状态**——现场模式/带电是**会变动的快照**
（历史踩过 TRAC 跟踪模式把 CH2 跟随出负压的坑，2026-09-13；同机 2026-10-08 已实测回到
NORM），不在此记录固定值；最新状态一律以工具实查为准，快照类信息归
`docs/DEVICE_FACTS.local.md`。收尾纪律不变：远程会话会把面板置 `REM`，工具收尾统一
`SYST:LOC` 交还。

## 五、分析方法选择（2026-09-09 教训）

**波形频率/周期分析优先用 FFT 或自相关，不要用朴素过零计数。**

案例：验证 SDS 波形时间轴时，用"上行过零点间隔中位数"算出周期 4997 点，
据此推断 interval 差 2 倍、采样率查询不可信。改用 FFT 后立刻定论：
主峰 100.00kHz（与设备硬件测量 100.045kHz 吻合）→ interval 1ns、采样率 1GSa/s
全部正确。

原因：被测信号是**调幅信号**（载波 100kHz + 20kHz 调制，谱图有 80/120kHz 对称边带），
包络使零点穿越不规则，朴素过零（无迟滞/去抖）会把每个载波周期数成多次。

若必须用过零：需加迟滞带（如中值的 ±5%）、去抖窗口，或先解调包络；纯正弦才可直接用。
**结论：不是"过零方法不可靠"，是朴素实现不可靠——复杂信号先用 FFT 定性。**

## 六、已知待办

- ~~MCP 工具按配置选择性加载~~ **已实现（2026-09-23，服务端 profile）**：见「七、runtime 分层与 MCP profile」。
  早先设想的"用客户端 `tools` 配置过滤"**只在部分客户端成立**——opencode 支持
  `tools` + glob，而 DSH 的 `dsh-mcp-client` 没有任何工具过滤字段（2026-09-23 查证），
  故服务端 profile 是多客户端下唯一可行方案。
- ~~sds_control 波形读取：PREamble DESC 布局不符~~ **已澄清（2026-09-09）**：
  DESC 解析完全正确——`interval`(1ns) 与 `ACQ:SRAT?`(1GSa/s) 一致，FFT 主频与设备
  硬件测量吻合，电压换算 Vpp 与测量值一致。此前"读出全零/interval 不可信"的判断
  源于两点：① 旧诊断在通道无信号时读取；② **用朴素过零计数验证调幅信号**（边带
  80/120kHz 导致每载波周期多次穿越，误判半周期）。教训见下方"分析工具选择"。
- ~~sds_control `get_waveform` 未暴露为 MCP 工具~~ **已完成（2026-09-09）**：
  新增 `sds_get_waveform(ch, points=50000, save_csv)`——返回摘要 + 可选 CSV 路径
  （不返回完整数组防上下文爆炸），FFT 交叉验证时间轴可信。
- waveform_matrix 遗留：带偏置信号（OFST≠0）的细调精度（居中残差×细调交互）
- ~~设备序列号/现场地址散落在文档与脚本里~~ **已收敛（2026-09-15）**：真实序列号
  统一移入 `docs/DEVICE_FACTS.local.md`（本机专有、已 gitignore），仓库内一律用
  `<serial>`/占位符；`TEST_DATA/` 整体不入库（体积 + 含序列号与网内 IP）。
- **看门狗/锁语义 → 已升级为统一异步调用边界**（2026-09-15）：所有工具经
  `@device_tool` 注册，由**单 worker 执行器**串行执行（MCP 层 async，设备 I/O 仍在
  worker 线程；50 个工具函数体一行未改）。语义：设备忙→立即 `device_busy`（不排队）；
  等待上限默认 150s（`sds_auto_scale` 300s，`INSTRUMENT_CALL_BUDGET_S` 可覆盖）；
  **超时 ≠ 操作终止**——worker 会跑完、设备保持 BUSY，之后自动恢复；事件循环不再被
  慢调用冻结。设计评审（含与网页版 GPT 的两轮辩论）见
  `docs/gpt_qa/20260915-mcp-async-refactor.md`。
- 串口探测**子进程隔离**：本进程线程探测会留下卡死线程，导致后续任何 VISA 调用打死
  服务器（2026-09-15 实测）。另：非串口批量识别须**共享单个 ResourceManager**——
  多线程各建 RM 会随机 `VI_ERROR_INV_OBJECT`（`common.discovery.identify_all`）。
- **`ks3458a_unstick` 对 Talk Only 的有效性待验证**（2026-09-23）：手册 p.159 只给面板
  处置（按 `Reset` 键 / 指定 31 以外的地址），原理上表"只听不讲"时 `IFC`/`RESET` 送不
  进去；但当日"重新拔插 82357B"与"面板按键"两个动作同时做过、**成因未能分离**，
  不能据此断言远程一定无效。下次遇到"每条命令都回一个电压读数"时，**分别**验证
  两条路径（面板 vs 远程 `ks3458a_unstick`）并把结果记入 `docs/TEST_RECORDS.md`，
  再据以定稿 docstring 与 skill。
- **`verify_session_lock.py` §6 在本机恒 FAIL（既有问题，非代码缺陷）**：该用例把锁目录
  指向 `Z:/definitely/not/writable` 模拟"不可写"，但**本机确有 Z: 盘且可写** → 前提失效、
  降级分支根本没被走到。已在改动前的 `47034be` 上复核为**同样失败**，与近期改动无关。
  修法：改用**保证不可写**的路径（如临时目录下先建一个同名文件、再把锁目录指到它的子路径），
  不要依赖"某个盘符不存在"。在修好之前，跑
  `TEST_SCRIPTS/common/regression_offline.ps1` 会稳定看到这 1 项 FAIL，其余套件应全绿。
- **架构评审结论（2026-09-23，与 GPT 两轮讨论；档案 `docs/gpt_qa/20260923-skill-dist-arch.md`）**：
  采纳的改进顺序 ——
  ① **工具可见集 147 → 46**：仪器类工具不再全局注册，改按项目/会话预设挂载。评审认为
     "候选集过载"的杠杆大于"单条 description 太长"，与本仓实测（窄接口工具调用率 100%
     vs 宽接口约 25%）一致；**需配套一个低成本的能力发现/切换机制**，避免 preset 切太窄
     导致模型发现不了它需要的工具。**（2026-10-08 注：① 已由 v2dev 的 compact profile
     实现并合入——见第七章；②~⑤ 仍未实施。）**
  ② **参数域交给 schema**：改用 Pydantic `Literal`/`Enum`（或 `Annotated[str, Field(...)]`）。
     当前参数 schema 只有 title/type，长枚举移进 skill 后接口退化成"无约束字符串"，会诱发
     幻觉参数。评审给的选择序：**schema Enum > description 列值 > `list_*` 运行时查询**；
     判据是调用频率 / 枚举稳定性 / 规模 / **错值代价**（错值会触发真实仪器动作时必须用 Enum）。
  ③ **工具 MSIC manifest + 规范句型 lint**：`Use when:` / `Do not use for:` / `Requires:` /
     `Values:` / `Returns:` / `See skill:`；机械查"契约完整性"，人工只审前两句（路由边界）。
  ④ **confusion set 静态回归**：把语义近邻工具分组并声明区分轴，断言组内每个工具至少有一个
     唯一判别项且 description 里真有对应信息（替代跑 LLM 的 routing 回归）。
  ⑤ **skill 两层化**：`SKILL.md`（能力地图 + 全局契约 + 高频路径 + **一跳索引**）+
     `references/*.md`，最多两层；判据＝"删掉这段后，模型还能否决定直接调用还是读哪个唯一的
     reference"。
  另两项与本仓现有实现直接相关：**link 生命周期要显式声明**（link=开发绑定 / copy=部署快照），
  copy 侧补 provenance，以便 `--check` 区分 OUTDATED / LOCALLY_MODIFIED / WRONG_SOURCE /
  BROKEN_LINK，并安全回收"仓库删除 skill 后遗留的孤儿链接"（当前有意不清理，见
  `verify_install_skills_uninstall.py` §7）；以及 **57 原子核对只证明文本守恒、不证明
  工具路由未退化**，需按 ④ 补行为侧的检查。
- **TS-ASTATION 远程 MCP 部署（2026-10-08 侦察，方案待定）**：3458A 在远程机
  （TS-ASTATION，PnP 82357B OK、preflight `ID?`→HP3458A 链路通）。远程机已有
  `C:\MySpace\AI\instrumentControl\`（仅 keysight_3458a + common 子集，无 git、无
  mcp_instruments）、Python 3.14.6 + pyvisa/numpy/psutil、**缺 mcp 包**。若做远程 MCP
  单例需：同步完整代码 + 装 mcp（Py3.14 兼容性待验证）+ 计划任务常驻（WinRM 会话内
  进程随会话死，skill 坑 #9）。**当前裁定：维持远程脚本模式**（远程跑采集脚本即可），
  MCP 化等真实使用频率证明后再定。

## 七、runtime 分层与 MCP profile（2026-09-23）

改仪器能力之前先读本节——它决定新代码该放哪一层。

```
mcp_instruments/            前端（MCP 专属）
  server.py                 legacy 68 工具 + profile 选择 + 执行器
  compact_tools.py          compact 4 工具（devices / search / describe / call）
instrument_runtime/         能力与护栏（**不依赖 MCP/FastMCP/pyvisa**）
  registry.py  catalog.py   操作登记 + 68 项声明式安全分类（risk/confirm/raw_scpi/verify）
  policy.py                 黑名单与查询判据（纯函数）
  broker.py                 放行口 decide_scpi() + 进程内设备锁
  audit.py  verify.py       审计落盘、回读配名、错误队列排空
  validate.py               按操作 schema 校验 instr_call 入参
  plan.py                   Batch Plan DSL v1：结构/作用域/规模 preflight（纯函数）
  batch.py                  batch 解释器（在**一个** executor job 内跑完整批）
*_control/                  设备库（既有，未改动）
```

**纪律**：`instrument_runtime/` 只依赖标准库。加设备逻辑放 `*_control/`；加护栏或
能力元数据放 `instrument_runtime/`；只有 MCP 协议相关的东西才放 `mcp_instruments/`。
`verify_broker_offline.py` 会在子进程里断言这条边界（import broker 不得牵入
mcp/fastmcp/pyvisa）。

**profile**（`--profile=<legacy|compact>` 或环境变量 `INSTRUMENT_MCP_PROFILE`，默认 `compact`）：

| | 工具数 | 工具定义体量 | 说明 |
|---|---|---|---|
| `compact` | 5 | 4600 字符 ≈ 1.5k token | **默认档**；比 legacy 省约 18.4k token/请求（92.3%） |
| `legacy` | 68 | 59677 字符 ≈ 19.9k token | 具名工具面；skill 与文档里的工具名都指这套 |

（体量由 `verify_compact_profile.py` 的 S4 现场测量，随描述文案变动。）

compact 的 5 个工具：`instr_devices`（列仪器）、`instr_search`（按关键词找操作）、
`instr_describe`（取完整参数表/说明/安全属性）、`instr_call`（执行一次操作）、
`instr_batch`（一次提交多个操作）。

### `instr_batch` 与 Batch Plan DSL v1

组合执行（扫频、批采、参数矩阵）用 `instr_batch(plan)`，plan 是**版本化** JSON
（`plan_version: 1`）。设计契约与取舍见
`docs/gpt_qa/2026-09-23-instrument-gateway-arch.md` 的 Q3/Q4；改这条路径前必须读。

**三条不许破的语义**：

1. **整批是一个 executor job**。BUSY 在整批期间保持，进程内其它设备调用拿到
   `device_busy` 而**不会插进序列中间**——扫频需要的正是这个。它保证的是进程内
   **non-interleaving**，**不是事务原子性**：不回滚，跨进程也不互斥。
   若改成"每个叶子各提交一个 job"，BUSY 会在步间释放，别的调用能插进来改频率。
2. **batch 自己不持 `_DEVICE_LOCK`**，叶子经 `_invoke_operation` 直接用 canonical
   operation 的实现（各自 `_call` 照常取锁，无嵌套 → 无死锁）。**绝不能**再走一次
   executor 或走公开的 `instr_call`（那是 executor 自调用）。
3. **deadline 是协作式的**：只在叶子**开始前**检查，到期不再启动新叶子，已启动的
   允许自然结束。上界是"最多再完成一个 canonical operation"——**不能**承诺"最多再发
   一条 SCPI"。底层 native 调用无法安全中断，声称"已取消"是错误事实。

**其它裁定**：变量用类型化引用 `{"$var": "名字"}`（不支持字符串模板）；capture 用整
result 或 RFC 6901 JSON Pointer，pointer 取不到就报 `capture_pointer_not_found`，
**绝不猜字段**；作用域严格且**静态拒绝**（foreach 内 capture 不能逃逸到外层、禁止
shadowing、未定义变量）；`max_leaf_steps` **精确计数但不展开**（超限时零仪器调用），
服务器另有硬上限；失败时**已产生的部分结果全部返回**（仪器侧副作用已发生，隐瞒会让
调用方无法判断设备状态）；capture 以**事件日志**返回而非扁平 dict（foreach 同名变量
在多轮中合法共存）；跨进程争用告警聚合到 run 级的 `contention` 字段并置
`integrity=contended`，但**不**把 `ok` 改成 false（既有口径是只告警、由调用方裁决）。

**两条不变量**（改 profile 相关代码时勿破）：
1. **两种 profile 都完整登记 68 个操作**，且 `instr_call` 与 legacy 同名工具
   **共用同一执行路径**（`_run_operation_by_name` 与 `device_tool` 的 wrapper 逐句等价）
   ——不这样就会出现"换 profile 后超时/device_busy 语义变了"这类极难查的差异。
2. **护栏不经前端放行**：黑名单在 `policy`/操作实现层，DG832 保护联锁在库里。
   绕过 compact 前端直接调 `instr_call` 同样跳不过任何门。前端只做入参校验。

新增工具必须同时在 `instrument_runtime/catalog.py` 登记风险等级，否则服务起不来
（未分类的工具不允许上线）。

### 发现（instr_discover / instr_devices）的成本控制（2026-09-23）

**全量发现很贵，别默认调它。** 实测本机 355.9s —— 两个 /16（公司网 + 仪器网）合计
**131,830 台主机**要扫；而且它经统一执行器跑，**BUSY 全程保持**，那 6 分钟里
**所有其它仪器调用都返回 `device_busy`**。它还超过 MCP 客户端默认的 60s
工具超时（实测客户端报 `-32001`）。

| 入口 | 行为 | 耗时 |
|---|---|---|
| `instr_devices()`（compact，默认） | 只读**配置 + 上次成功缓存**，不扫描 | **0.03s** |
| `instr_devices(full=true)` | 全量发现（扫描各网段 + 所有 VISA 接口） | 355.9s（本机） |
| `instr_discover`（legacy） | 全量发现（**原语义未改**） | 同上 |

地址解析（`common/resolver.py`）的顺序仍是：**显式入参 → 环境变量 → `devices.json`
→ 上次成功缓存 `last_good_resources.json` → 自动发现**。所以常规设备操作走缓存
（`dg_status` 168ms），**只有显式"发现"才全扫**。

**排除不需要扫的网段**（本机最有效的一招：公司网那侧不会有仪器）：

```
# 环境变量（逗号/分号分隔）
INSTRUMENT_EXCLUDE_CIDRS=172.29.0.0/16,172.16.80.0/24
# 或写进 %LOCALAPPDATA%\instrumentControl\devices.json 的 exclude_cidrs 数组
```

本机加上这两条后待扫主机从 **131,830 → 65,788**。排除**只作用于扫描**，
已配置/已缓存的地址不受影响；写错的 CIDR 会被忽略而不是拖垮扫描。

**串口探测**已收紧到内层 0.5s、外层硬杀 2s（原 2s/8s），6 个空口全量探测
10s → **1.85s**。外层刻意不是 0.5s：它要覆盖 python 子进程启动（Windows 约
0.3~0.5s），设成 0.5s 会把好设备也误判成"驱动挂起"。

未做但值得做（按性价比）：宽扫改**后台任务 + 缓存**（不再占 BUSY）、
**L2 广播发现**（VXI-11 portmapper，把 O(主机数) 变成 O(1) 个包，需先确认仪器是否
响应广播）、有界并发 + 超时重试（现有并发上限是硬编码的 256，注释里记着无限并发
会导致假阴性）。

校验脚本（全部离线，不碰仪器）：
`verify_registry_parity.py`（工具表逐字节一致 + 分类覆盖）、`verify_broker_offline.py`
（分层边界 + 判据等价）、`verify_compact_profile.py`（compact 形态 + 能力不丢 + 成本）、
`verify_batch_offline.py`（plan preflight / 作用域 / 规模 / 指针 / 执行 / **"整批一个 job"**
这条核心不变量）、`verify_compact_mcp.py`（**协议级**：起真实子进程走 JSON-RPC，
确认客户端真能拿到 5 个工具并调用成功）、`dump_mcp_tools.py`（固化 tools/list 快照）。

### 在 DSH 上把 compact 切成默认（切换与回滚）

目标文件 `~/.dsh/profiles/web/cordis.patch.yml` 属**外部目录**——按仓库规则不由 AI 代改，
由使用者执行。用仓内脚本，它负责备份、精确定位、YAML 校验与幂等（**逐行文本编辑**，
不做 YAML 往返：往返会抹掉这个文件里一半的价值——注释）：

```bash
python TEST_SCRIPTS/common/switch_dsh_instrument_profile.py --profile compact --dry-run
python TEST_SCRIPTS/common/switch_dsh_instrument_profile.py --profile compact --apply
# 回滚
python TEST_SCRIPTS/common/switch_dsh_instrument_profile.py --profile legacy --apply
```

它做的就是在 `mcp-instrument` 条目的 `env:` 下加/删一行 `INSTRUMENT_MCP_PROFILE`。
compact 已是**代码默认档**，所以正常情况下配置里不需要这一行；脚本现在的用途是
把某一档**显式钉死**（例如回退 `--profile legacy --apply`），或临时试验另一档。

**切换后必须重启 DSH**：MCP 子进程的工具表不会热重载（HMR 只重载插件配置），
与 `cordis.patch.yml` 里既有的那条注记同因。重启会中断当前会话，故这一步只能由人做。

校验套件（全部离线、不碰仪器，除最后一条）：

```bash
python TEST_SCRIPTS/common/verify_registry_parity.py   # registry <-> legacy 工具表 + 基线快照
python TEST_SCRIPTS/common/verify_broker_offline.py    # 护栏/锁的唯一真源
python TEST_SCRIPTS/common/verify_compact_profile.py   # 进程内：形态/能力/成本
python TEST_SCRIPTS/common/verify_batch_offline.py     # Batch Plan DSL 语义
python TEST_SCRIPTS/common/verify_compact_mcp.py       # 协议级 + 默认档 + 设备路径（死回环）
python TEST_SCRIPTS/common/verify_mcp_tools_meta.py    # legacy 工具表元数据
python TEST_SCRIPTS/common/audit_guardrail_coverage.py # 黑名单误伤 / 白名单出处
python TEST_SCRIPTS/common/dump_mcp_tools.py           # 固化线上工具表快照
```

**校验脚本自己钉 profile**：断言 legacy 具名工具面的那几个（`verify_registry_parity`、
`verify_compact_profile`、`verify_mcp_tools_meta`、`dump_mcp_tools`、`verify_session_lock_live`、
`ks3458a/` 下两个）在 import / spawn 之前**显式**设 `INSTRUMENT_MCP_PROFILE=legacy`。
默认档改动后若不这样钉，它们会静默对着 compact 的 5 个工具做断言——要么失败得莫名其妙，
要么（成本对比那种）算出没有意义的数字。`verify_compact_mcp.py` 的 S0 反过来测默认档：
它把环境变量**摘掉**（而不是设成 compact），否则测的是环境变量而不是 `_DEFAULT_PROFILE`。

FastMCP 派生漂移（`Tool.from_function` 的结果与真实注册对象不一致）只在 legacy 下做启动期
比对——compact 的服务实例里没有这 68 个工具，无从比对。所以这道守卫现在由
`verify_registry_parity.py` 承担（对着冻结快照逐字节断言），**FastMCP 升级后必须跑它**。

切换前后的**真实对比数据**（读 DSH 会话日志里那次请求真正带了什么）：

```bash
python TEST_SCRIPTS/common/measure_dsh_tool_cost.py "--workspace=--D-ChatWorkspace--" --out before.json
# 重启后
python TEST_SCRIPTS/common/measure_dsh_tool_cost.py "--workspace=--D-ChatWorkspace--" --out after.json
python TEST_SCRIPTS/common/measure_dsh_tool_cost.py --compare before.json after.json
```

实测切换前基线（真实会话，非合成快照）：整个工具表 108 个 / 92629 字符 ≈ 30876 token，
其中 `mcp__instrument__*` 57 个 / 40991 字符 ≈ 13663 token = **44.4%**。

另一组是**合成对比**（`verify_compact_profile.py` 的 S4 现场测量，只看仪器这一组）：
68 个工具 59,677 字符 ≈ 19.9k token → 5 个工具 4,600 字符 ≈ 1.5k token，省约 18.4k。
**两组数字不可混用**：会话那一组是当时实际注册的工具集，且整个工具表里还有其它
MCP 服务器的工具。

回滚不留不一致状态：legacy 的 68 个工具名与 schema 在任何阶段都**未变过**
（`verify_registry_parity.py` 对着基线快照逐字节断言）。

**当前状态**：compact 是**代码默认档**，任何地方都不必再配 profile（`cordis.patch.yml` 里
那行 `INSTRUMENT_MCP_PROFILE: compact` 已属冗余，留着无害）。静态工具定义之外的对比数据
（总输入 token、工具选择失败率、参数错误率、往返次数）需要重启用真实会话跑一轮才有。

设计与取舍见 `docs/gpt_qa/2026-09-23-instrument-gateway-arch.md`。
