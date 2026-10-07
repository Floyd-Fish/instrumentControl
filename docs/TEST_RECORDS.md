# 实测记录（从 README 迁出的时间线）

> 本文保存 instrumentControl 的逐轮实测留痕与结论，按时间倒序/正序累积。
> README 只保留项目定位与用法，**测试/实测细节一律记在这里**。
> 新记录追加到文末，格式：`- YYYY-MM-DD：结论（证据文件路径）`。
>
> **地址说明**：下文出现的 IP / 资源串 / 串口号 / USB 序列号均为**当时实测值**，
> 不是设备固定资产——会随 DHCP 续租、换网段、换 USB 口、串口号漂移而变化。
> 接入前一律先 `instr_discover`（或 `resolve(kind)`）重新定位，**勿直接照抄**。
>
> **留痕说明（2026-09-15 起）**：`TEST_DATA/` 整体不入库（体积 + 含设备序列号与
> 网内 IP），新留痕只落在本机工作区。**下文引用的历史证据文件不在仓库里**，
> 需要时从历史提交取回：`git show <commit>:<路径>`，例如
> `git show 7e28880^:TEST_DATA/dho/dho_first_verify_20260824_112452.json`。

- 2026-08-17：DH1766A-1 识别为 `USB0::0x0957::0xA007::<serial>::INSTR`，
  `*IDN?` = `BJDH,DH1766A-1,0,V0.1.4.3`；三路读回电压/电流正常，输出开关命令验证通过。
  注：该设备 VID=0x0957（Keysight ID），为国产仪器兼容 VISA 驱动常见做法，以 `*IDN?` 为准。
- 2026-08-17：手册 4.2 全部指令集 40/40 读写验证通过（`test_dh1766_full.py`，留痕见
  `TEST_DATA/dh1766/dh1766_full_*.json`）。全部写操作备份-恢复，设备设定与测试前一致。
  实测发现的固件差异（V0.1.4.3 vs 手册基于的 V0.1.2.8）见 `dh1766_control/docs/EXPERIENCE.md`。
- 2026-08-17：`dh1766_control` 库建立（src 布局，pip 可安装），DH1766 系列完整手册
  （43 页）经 read-pdf 提取归档至 `dh1766_control/docs/`，供后续查阅核对。
- 2026-08-23：`common/` 统一发现层上线（`find_device`：显式 resource → hosts 自动选协议 →
  已有资源列表 → CIDR 网段扫描，`--allow-scan` 默认关）。实测要点：纯 VISA 扫 /24 需 510s，
  加 TCP 端口预筛(111/4880/5025/5555) 后 ~8s；DH1766A-1 经网线可达
  `TCPIP0::192.168.31.144::5025::SOCKET`（raw socket 会话必须配 `\n` 终止符）；
  RIGOL DHO924S 示波器可达 `TCPIP0::192.168.31.146::5555::SOCKET`（Rigol SCPI raw 口）。
  带载(CH1 ON 12V/0.31A)下显式 LAN 直连读取正常，CH1 保持 ON 未做开关动作。留痕见
  `TEST_DATA/common/discovery_smoke_*.json` 与 `TEST_SCRIPTS/common/test_discovery.py`
  （T1~T5 全 PASS，扫描同时识别电源+示波器且 IDN 过滤不误配）。
- 2026-08-23：全网段探测（`TEST_SCRIPTS/common/probe_all.py`）新发现三台在线仪器：
  Keysight 34465A 万用表(`.123`，VXI-11)、Siglent SDG2122X 信号源(`.206`，VXI-11)、
  Siglent SDS824X HD 示波器(`.220`，VXI-11)。`dho_control` 库建立（DHO800/DHO900 系列
  通用，波形 BYTE/WORD TMC 解析 + 电压换算 `(raw-YORigin-YREFerence)*YINCrement`），
  手册提取至 `dho_control/docs/DHO800编程手册_output/`（418 页），hosts 直连验证通过。
- 2026-08-23：三台新设备库完成（均含手册提取 + 只读冒烟实测）：
  `sds_control`（SDS800X HD 系列，495 页手册，波形 PREamble 二进制协议+分片读取+电压换算
  raw/code*vdiv-offset）、`sdg_control`（SDG2000X 系列，175 页 PG，BSWV 整查/键值写）、
  `keysight_3446x`（Truevolt 583 页手册已下载提取，CONF/MEAS/NPLC/DATA:LAST）。
  实测要点：SDS 全拼命令 ":ACQuire:MDEPth?" 不响应必须短形式 "ACQ:MDEP?"；SDS 响应带单位
  后缀需剥离；SDS 查询回显头按前缀智能剥离；34465A DATA:LAST? 带 "VDC" 后缀。
  留痕：TEST_DATA/common/three_libs_smoke_*.json。DHO924S 待设备空闲后做全功能验证。
  （2026-09-13 回填：DHO 全功能验证已于 2026-08-24 完成——留痕
  `TEST_DATA/dho/dho_first_verify_20260824_112452.json`（*IDN?/snapshot/波形 CH1）与
  `dho_write_verify_20260824_112935.json`（通道/时基/触发写入+恢复比对）。）
- 2026-08-25：`mcp_instruments/` MCP 服务器上线：五台仪器 17 工具统一暴露（无状态
  连接→操作→关闭 + 全局锁串行化；复位类零暴露，关机/开输出 confirm=True 安全门；
  错误统一 `{ok, error_type, error}` 四分类）。已注册 zcode 用户级 config
  （`~/.zcode/cli/config.json` 的 `instruments`，新会话生效；使用指引 skill：instrument-mcp）。
- 2026-08-26：`instr_discover` v3（串口探测：占用提示/空闲 IDN 后断开/驱动挂起 6s 硬超时；
  VISA 先于 LAN 隔离代理干扰；fake-IP 网段污染降级 warning），实测发现串口新设备
  EmoeCalibrator（ASRL31），建 `emoe_control` 骨架库（仅发现 + `*IDN?`，编程手册未提供）。
- 2026-08-26/09-01：三轮审查修复（整体代码审查 17 项、MCP 专项、DH1766 远控文档建议：
  `find_dh1766` 上次成功地址缓存（自 2026-09-13 起写在用户级缓存目录
  `%LOCALAPPDATA%\instrumentControl\last_good_resource.json`，不再落包源码树）、
  `power_cycle` 高层 API——后者未暴露 MCP）。
- 2026-09-03：MCP 增至 19 工具（**注：2026-09-13 已增至 31 = 28 专用 + 3 通用护栏，
  见 `mcp_instruments/README.md`**）：新增 `instr_query`/`instr_write` 通用护栏（新设备零代码
  接入——复位类黑名单 forbidden、通用写 confirm=True、写前 drain/写后 SYST:ERR?/自动
  回读、审计落盘 `TEST_DATA/common/mcp_scpi_audit_*.jsonl`、离线资源硬超时看门狗）。
  实测修复三缺陷：(a) `mcp.run()` 事件循环与后台线程 import pyvisa 死锁（冷进程首个
  设备调用永久冻结）→ 重依赖 import 移主线程（<1s）；(b) `instr_discover` LAN 分支
  `probe_alive`/`identify_lan` 未导入 NameError（存量 bug）→ 补导入+LAN 异常降级；
  (c) 串口探测每线程各建 RM 原生崩溃（8 串口实测）→ 共享单例 RM。真机验证：SDG 整查/
  等值写三步闭环（状态零变更）、串口新设备发现并零接入识别 EmoeR&D ADS127L11-DAQ-EV
  （ASRL5；校准器 ASRL31 已离线/换号，串口号会漂移，接入先 `instr_discover` 重定位）。
- 2026-09-13：**DH1766 远程模式核实**（用户报告"一连就被锁"）：
  `SYST:COMM:RLST:STAT?`（手册写法）在本机 V0.1.4.3 **无响应**（超时，此前文档误记"空串"）；
  正确查询 `SYST:COMM:RLST?` 实测**每次新建远程会话的第一条命令即返回 `REM`**
  ——即"只要远程连接就进入远程模式"成立；发 `SYST:LOC` 后立即回到 `LOC`（面板控制权可交还，
  不影响输出/设定）。同时实测该机处于 **TRAC 跟踪模式**（CH1 +11.99V / CH2 −11.99V，
  CH1 0.39A 带载）——跟踪负压是正常跟随，非故障。
  留痕：`TEST_SCRIPTS/dh1766/psu_remote_lock_probe.py` +
  `TEST_DATA/dh1766/psu_lock_probe_20260913_*.json`；
  代码修正：`commands.py::SYST_RLST`、`dh1766.py::rlstate()`、MCP `_psu_close`（收尾发 `SYST:LOC`）。
  现场复原：探测结束后已 `*CLS` 清空自造错误队列、`SYST:LOC` 归还面板；
  输出状态与设定值与探测前一致（`APPL:OUTP?`=`1,1,0`，12/12/5V，1.5/1.5/1.5A）——**零改动**。
- 2026-09-13（同日，维护轮）：安全与工程收口——
  ① 黑名单补齐 `SYST:RWL` / `:SYST:COMM:RLST`（原正则只覆盖 `REM/REMOTE/LOCK`，
  DH1766 锁定命令会漏放），并改为"**纯查询放行、写一律拦**"；
  回归 `TEST_SCRIPTS/common/verify_remote_lock_block.py` 24/24 PASS（离线）；
  ② 测试脚本铁律修复：`test_dh1766_read.py`（try/finally + 带电跳过演示）、
  `test_dho_write.py`（恢复动作移入 finally）、`test_sds_shutdown.py`（加 `--yes` 门槛）；
  ③ `last_good_resource.json` 迁出包源码树 → `%LOCALAPPDATA%\instrumentControl\`；
  ④ `.gitignore` 收口 MCP 运行期产物（波形 CSV 332MB / 截屏 PNG / 审计 jsonl / 旧 BMP）；
  ⑤ README 瘦身（实测时间线移入本文件）+ 文档漂移专项审计
  （报告：`docs/doc_drift_audit_20260913.md`，修正 8 份文档）。
- 2026-09-15：**新增 RIGOL MHO900（MHO984D）示波器接入**（本机只挂这一台，其余五台不在
  本实验台可达范围内——`instr_discover` 只识别到 `192.168.1.55` 的 MHO984D，
  `recognised_now={"mho": …}`，其余 kind 报"未确定资源地址"属环境事实）。
  - 手册提取：`mho_control/docs/MHO900编程手册_output/`（480 页，654 条命令 + 索引 + 说明）
    与 `docs/MHO900用户手册_摘录.md`（279 页摘录：本系列 4 通道 12bit、1~2ch 800MHz/4GSa/s、
    3~4ch 400MHz/1GSa/s、100Mpts 标准深度）。
  - 库：`mho_control/`（commands.py 47 条命令 + mho.py）；解析 kind `mho`；MCP 6 个 `mho_*` 工具
    （服务器 31→37 工具）。
  - 实测（`TEST_SCRIPTS/mho/verify_mho.py`，留痕 `TEST_DATA/mho/verify_mho_20260915_*.json`，
    **45/45 PASS**）：快照/测量（单+双信源）/三格式波形/写回读恢复/截屏 PNG/RAW 50000 点
    （STOP→读→恢复 RUN）/MCP 工具层。关键交叉验证：**冻结态 BYTE vs WORD Vpp 差 0.196%**
    → WORD 低字节在前成立（手册未记载字节序）；RAW `xinc=5e-10s` 与 2GSa/s 采样率自洽。
  - 命令审计：`audit_all_commands.py` 新增 mho TARGETS，47 HIT / 4 MISS，4 条 MISS 逐条核对
    手册为**审计器归一化假阳性**（`:CLEar` 3.1.1 / `:SINGle` 3.1.4 / `:TFORce` 3.1.5 /
    `:AUToset` 3.2.1，单段根命令未入索引），代码中无猜测命令。
  - 安全加固（真机前发现的绕过，已修并回归 `verify_remote_lock_block.py` 47 用例全 PASS）：
    ① `instr_query` 原只查 `?` 不查黑名单 → `"*RST;*IDN?"` 可从只读口复位仪器；
    ② `instr_write.readback_cmd` 同样未查黑名单；③ 黑名单只比对长短形式，`SYST:RESE`/
    `SYST:PRESE`/`SYST:COMMU:RLST` 等中间缩写漏网。另把 MHO 的 `:SYSTem:LOCKed` 纳入同族拦截。
  - 审阅（缺陷与设计问题，含真机前验证的 repo 既有缺陷）：`docs/review_20260915.md`。
- 2026-09-15（同日，工具链轮）：**命令审计器重构 + 离线闭环自测 + DH1766 专项审计**。
  - 审计器两处结构性缺陷修复：① 旧版只扫"以 `:`/`*` 开头"的字符串字面量，
    `{ch}:OUTP?`/`C{n}:VDIV?`/`TDIV?`/`PRIN? BMP` 这类常量**全在盲区**
    （sdg_control/commands.py 旧版只能抽出 2 条，实际 9 条）；② 旧归一化逐段截前 4 字符，
    造成 ~30 条稳定假阳性（Keysight 短形式、`:CLEar`/`:AUToset` 类单段根命令全判 MISS）。
    新判据：AST 提取（排除 docstring、还原 f-string 被插值切断的命令）+ 段键元组归一
    （逐段取 SCPI 短形式、剥通道选择器）+ 段内长短形式兼容 + 后缀路径命中 + f-string
    拼接的命令名归 DYN 不计 MISS；手册索引补"无前导冒号冒号链"（Keysight 体例）。
  - 效果：全仓 MISS 从 35（旧口径，含 ~30 假阳性）收敛到 **27 条且条条可解释**：
    dh1766 8（STAT 组，出处=原版手册，真机已验证）/ sds 6（旧短形式别名 + `SYST:ERR?`）
    / sdg 3 / k3446x 1（`READ?`）/ 探针脚本 9（**故意**发待验证命令）/ dho·mho 0。
  - **离线闭环自测**：`TEST_SCRIPTS/common/verify_audit_extractor.py`（60+ 断言：
    提取层 18 + 归一化 14 + 全仓 MISS 基线 7 组 + 必须命中 27 条）**全 PASS**，
    无仪器可跑；"多/少一条 MISS"的失败场景已验证会 FAIL。
  - **DH1766 专项审计**：`docs/dh1766_audit_20260915.md`（本机无该电源，纯静态审计）：
    H1 `safe_mode` 可被 `apply_voltage()/apply_current()` 旁路（唯一写保护失效，已复核
    守卫调用点仅 186/210/255/276 四处）；H2 `rst()` 裸暴露 `*RST`；M1 `remote()/rwlock()`
    库层可直呼且 `test_dh1766_full.py:218` 已直呼；M2 `set_output` 写后不回读；
    M3 `power_cycle` 无 try/finally；M4 8 条 `STAT:*` 仓内无完整出处。含离线补丁与
    真机回归清单 T1~T8，等仪器回来执行。
- 2026-09-15（同日，DG832 合并轮）：**DG832 信号源并入 instrumentControl + MCP 安装到 zcode**。
  - **合并**：库/笔记/手册文本进仓（`dg832_control/`，驱动 `dg832.py` **逐字节未改**，
    md5 `d1e33622d2a865a1fa6661f0b3c0bd4c`）；`resolve("dg")` 新 kind（IDN 匹配 `DG8`）；
    MCP 12 个 `dg_*` 工具并入统一服务器（37→**49 工具 = 46 专用 + 3 通用**；
    原独立 `instrument` 服务器的 13 个 `instrument_*` 工具退役，`instrument_discover` 由
    仓库既有 `instr_discover` 覆盖）。并入时为 `dg_output` 补上仓库红线要求的
    **开/关都需 confirm=True**（其余保护联锁/DC 快照/扫频语义原样保留）。
  - **旧副本处置**：同一份驱动此前三处并存（工作区、skill、仓库）。skill 内 `scripts/`
    两份副本已删（删除前 md5 与仓库一致）并留 `RETIRED.md` 指向新位置；skill 的
    SKILL.md 全量改指仓库 + `dg_*` 工具名（**playbook 内容原样保留**：SOP/踩坑/排障）；
    `D:\ChatWorkspace\DG832使用\mcp_dg832\` 留 `RETIRED.md` 说明（未删用户文件）。
    `AGENTS.md` 待办"skill/scripts 双副本需人工同步"关闭。
  - **真机验收（只读）**：`TEST_SCRIPTS/dg832/verify_dg832.py` → **15/15 PASS**
    （留痕 `TEST_DATA/dg832/verify_dg832_20260915_105953.json`）。实测：`resolve("dg")`
    经 VISA 列表层自动发现 `USB0::0x1AB1::0x0643::<serial>::INSTR`（无需写死地址）；
    CH1 SIN 1kHz 输出 OFF、CH1 保护 ON（high 4V/low −2.5V）、CH2 SQU 50kHz 输出 OFF；
    多命令走私 `dg_query("*IDN?;*RST")` 被拒；`dg_output` 无 confirm 被拒。
    **写路径（改频率→回读→恢复）在 `--allow-write` 下可用，本轮未跑**（现场输出状态未知，
    未获改动授权）；`dg_output` 真机开/关同样待授权。
  - **命令审计**：审计器新增 dg832 目标（参照物 `dg832_control/docs/02_编程手册.txt`）→
    **32 HIT / 0 MISS**（驱动命令全部可回溯手册）；闭环自测
    `verify_audit_extractor.py` 增加 dg832 基线（0 MISS + 8 条必须命中）全 PASS；
    全仓 MISS 总数仍为 27。
  - **MCP 安装到本机 zcode**：`~/.zcode/cli/config.json` 注册 `instruments`
    （python 全路径 + 仓库 server.py，配置已备份 `config.json.bak-20260915`）；
    **stdio 握手实测通过**：`initialize` → serverInfo `instruments` v1.29.0，
    `tools/list` → 49 工具（sds 13 / dg 12 / mho 6 / psu 5 / sdg 4 / dmm 4 / dho 2 / instr 3）。
- 2026-09-15（同日，MHO/DHO 合并轮）：**确认两系列命令集一致并完成功能合并**。
  - 比对结论（`docs/rigol_scope_compare_20260915.md`）：DHO800/900 手册 908 个命令键、
    MHO900 1051 个，**共有 881 = DHO 的 97%**；驱动视角更直接——**DHO 驱动用到的 40 条命令
    100% 存在于 MHO 手册**，MHO 驱动 45 条中仅 2 条为 MHO 专有（`:ACQuire:BITS`、
    `:CHANnel<n>:Impedance`）。真差异只有：清测量（DHO `:MEASure:CLEar` / MHO `:MEASure:DELete`）、
    采集第四态（`ULTRa` / `HRESolution`）、两条 MHO 专有命令。
  - **更正一处既有笔误**：边沿第三态**两系列手册都写 `RFALl`**（DHO `RFALl`×12、`RFail`×0），
    `RFail` 出自 `dho.py` docstring，此前被当作"系列差异"写进 AGENTS.md——已按手册原文更正。
    教训：文档互相抄写会把笔误固化，**以手册原文为准**（已写入该文档）。
  - **合并实施**：新增共享内核 `rigol_scope/`（`scope.py` 通用实现 + `families.py` 家族差异表），
    `dho_control.DHO`/`mho_control.MHO` 变薄封装（公开 API 不变）；DHO 由此补齐双信源测量、
    波形分片、points/RAW 校验、原生 PNG 截图，并修掉 ASCII 死分支、删除 `reset()`
    （`:SYSTem:RESet` 属禁发命令）。
  - **验证**：离线 `verify_rigol_scope_shared.py` 74 断言全 PASS（假传输回放两家族响应）；
    真机 `verify_mho.py --allow-stop` **45/45 PASS**；审计器新增 rigol_scope 组
    （并集判）37 HIT/1 MISS（MISS 是否定性说明文字，非命令）。
  - **实测教训（已升为 AGENTS 铁律#9）**：`BYTE` 波形 2V/div 下 0.0683 V/code（WORD 的 256 倍），
    小信号仅占 3 码 → 与设备测量比会差 20%+；**量值用 WORD**（WORD/ASCii 实测差 0.00%）。
  - **待办**：DHO 真机复验（本实验台无 DHO，LAN 扫描只有 MHO）——清单见
    `verify_rigol_scope_shared.py` 头部 6 条。
- 2026-09-15（同日，护栏修正）：**查询判据改按 SCPI 规范**。用户报障：
  `instr_query` 把带参数的查询拒了。核实：SCPI 允许 `<header>? <param>`，
  两个 RIGOL 系列手册的实例都是 `:MEASure:ITEM? VPP,CHANnel2` 这种形态，
  Keysight 也有 `SAMPle:COUNt? MAX`——而上一轮修走私通道时我把判据写成
  "整段以 `?` 结尾"，等于把这类**合法查询**一起拒了（当时用例里没有带参数的查询，
  所以没暴露）。现判据＝每条 `;` 分段的**命令头**里第一个 `?` 之前是合法助记符
  （允许前导 `:`/`*`）；同时把公共命令族（`*RST/*SAV/*RCL`）的检查从"只看命令头"
  放宽到**整段搜索**作为纵深防御（参数位置偷发复位也拦）。回归
  `TEST_SCRIPTS/common/verify_remote_lock_block.py` 新增 §6（7 条带参数查询必须放行）
  与 §7（3 条偷发仍拦）——这两个盲区此前没有用例覆盖。
- 2026-09-15（同日，规范核实轮）：**核实「分号能不能把写命令塞进查询」**。用户质疑该护栏是
  无用代码（认为 SCPI 靠换行符划分命令）。结论：**护栏有效，但实现可简化一半**。
  - 规范：Keysight Truevolt 手册（仓内）明文——`A semicolon ( ; ) separates commands within the same
    subsystem… TRIG:SOUR EXT;COUNT 10 is equivalent to the following two commands`；换行符结束的是整条
    **消息**，分号在消息内分隔**命令单元**。RIGOL/Siglent 手册无相关说明。
  - **真机判定（DG832，空闲）**：① `:SOUR1:APPL?;:SOUR2:APPL?` → 同一行返回 `"DC,…";"SQU,…"`
    （两个单元都被执行）；② `:SOUR1:PHAS?;:SOUR1:PHAS 123` → 查询应答 0，随后读 PHAS **= 123**：
    **夹带的写命令确实生效**。留痕 `TEST_SCRIPTS/dg832/probe_semicolon_units.py` +
    `TEST_DATA/dg832/semicolon_units_probe_20260915.json`。
  - **据此简化**：查询口判据由「逐段解析助记符」改为**一条正则**（单条命令单元 + 命令头以 `?` 结尾、
    问号后可带参数），`_segment_is_query`/`_MNEMONIC_HEAD_RE` 删除；多单元查询（即使全是查询）
    现一律拒，调用方拆分即可（本仓库代码**从未**发过多单元消息）。写路径保留**逐单元**黑名单扫描
    （写命令天然会用分号串联，Keysight 例子即如此）。
  - 回归：`verify_remote_lock_block.py` 扩到 **67 用例**（§4 多单元查询须拒、§7 补两条），全 PASS。
- 2026-09-15（同日，二次回归修正）：**多段纯查询被判 forbidden**。用户报障：
  `readback_cmd=":CHANnel4:DISPlay?;:CHANnel4:SCALe?;:CHANnel4:OFFSet?"` 被判 forbidden，
  而几小时前可用。原因确认：上一轮我把查询判据从"逐段检查"收紧成"只允许单条命令单元"
  （为简化代码），**误伤了多段回读**这一合法常用写法。
  - 修正：判据回到**逐段**——每段都必须是查询单元（命令头以 `?` 结尾、问号后可带参数，
    `_QUERY_UNIT_RE`），多段纯查询放行；只要一段是写/复位/锁定就整条拒。
    实现仍是"一条正则 + 逐段套用"，不回到助记符解析那套（代码量不变）。
  - 回归 `verify_remote_lock_block.py` → **70 用例全 PASS**：§4 改为"多段纯查询必须放行"
    （5 条，含用户报障那条三段回读，同时断言 `instr_query` 与 `readback_cmd` 都不拦）；
    §3/§7 保留夹带写、复位/锁定的拦截用例（新增 `:CHANnel4:DISPlay?;:OUTP4 ON`）。
  - 文档口径（AGENTS/MCP README/mcp SKILL/review/HTML 汇报）同步为"逐段判、多段纯查询放行"。
- 2026-09-15（同日，护栏覆盖性审计轮）：新增 `TEST_SCRIPTS/common/audit_guardrail_coverage.py`
  —— 把"改护栏只验证想拦的、没验证不想拦的"自动化：§A 用**六套手册的全部命令**（约 7900 条）
  喂黑名单查误伤；§B 每条命令生成 `:CMD?` / `:CMD? MAX` 查查询判据；§C/§D 把各工具的**入参白名单**
  与手册枚举**双向**比对（手册有我们缺 = 会误拦）。
  - **首轮抓到并修掉 3 个真缺陷**（均属"白名单抄写/打字未核对"）：
    `sds_control.SDS.MEAS_TYPES` 的 `RISE20T90` → 手册是 `RISE20T80`（真项被误拦+错项被误放）；
    `rigol_scope` DHO 触发类型误抄 MHO 列表（多出 IIS/FLEXray/M1554，M1554 两手册皆无）；
    `rigol_scope` DHO 测量项缺 `ACRMs`（会误拦合法测量）。
  - 审计脚本自身也修了两处启发式缺陷：提取残留过滤要按字符类（`{"OFF"|"CALCulate:DATA"}`
    曾漏过 2 条）；item 枚举要合并"分页切成两块"的续块（曾误判 MHO 缺 `FFDelay`）。
  - 现状：§A 0 / §B 0 / §C 0 / §D 0（留痕 `TEST_DATA/common/guardrail_coverage_*.json`）；
    SDS 那侧的 item 表因提取形态是表格、§D 跳过（由 §C + 显式 diff 把关，已说明）。

- 2026-09-16：**按现场文档补齐示波器"设置类"工具与读数诊断**（来源：E_distance 项目的
  《示波器使用要点（MHO984D）》与其指向的两份设计文档 `tool_optimization_20260915.md` /
  `示波器自动定标设计-2026-09-15.md`）：
  - 新增 MCP 工具 `mho_channel` / `mho_timebase` / `mho_trigger` / `mho_fit_channel`
    （+ DHO 的 channel/timebase/trigger），工具总数 50 → **57**；内核侧新增
    `configure_channel/configure_timebase/configure_trigger/measure_stats/diagnose_no_reading/fit_channel`
    与 `Family.vdivs/hdivs/adc_bits/scale_range`、`vertical_window/horizontal_window`。
  - 把三条**现场实测设备行为**固化成代码语义：中心 = −offset（MHO 实测 8 格）、
    改 `SCALe` 会等比缩放 `offset`（故固定 **scale→offset** 顺序）、通道 OFF 时写垂直参数
    **被静默忽略**（故需要时自动先开）、偏置量程 ±20 V（越界报 `adjusted`+`reasons`，不混成成功）。
  - 读数诊断：无有效值分五类（channel_off / off_screen / near_edge / few_edges / no_signal）
    带 `hint`/`window`/`evidence`；`samples=N` 主机侧统计；测量返回带 `probe_x`。
  - **离线回归**：`TEST_SCRIPTS/common/verify_rigol_scope_semantics.py`（24 项断言，全部 PASS）
    —— 用设备模型 `FakeTrapScope` 复现三条陷阱 + "信号↔窗口"关系，并复现 scale 0.9/1.0 的
    标定证据（上沿 7.1 V 读不到 / 7.5 V 读到）。
  - 未做（已在设计文档 §5 说明）：SDS 侧设置类工具（其"中心/格数"约定未实测，不照抄）；
    DHO 格数未标定 → `dho_fit_channel` 不暴露、`dho_*` 写路径未实机验证。
  - 复跑：`verify_rigol_scope_shared` / `verify_remote_lock_block(70)` / `verify_audit_extractor`
    全 PASS；`audit_all_commands` MISS 29（基线）；`audit_guardrail_coverage` §A-D 全 0；
    MCP 握手 57 工具 + 启动自检行（查询判据实测值打到 stderr）。

- 2026-09-16（同日，补"顶/底轨分别提示"）：`rails()`（读 VTOP/VBASe + 分顶/底贴边判定）
  + `diagnose_no_reading` 的 `near_edge` 拆成 `edges_touching`/`edge_hints`（顶贴与底贴
  处理方向相反：顶贴→offset 调更负、底贴→offset 调更大）+ `mho/dho_measure_item(rails=True)`。
  **未用新工具实现**（并入既有测量工具，避免工具数膨胀）。回归补 7 项（顶贴/底贴/两端贴/
  无贴边/诊断分顶底/DHO 未标定不猜），`verify_rigol_scope_semantics` 共 **31 项全 PASS**。

- 2026-09-16（同日，工具元信息核查）：用户报"instrument MCP 12 个工具缺 description
  （49 个里 37 个有）"。逐版核对：**统一服务器 57/57 工具全有描述**（`tools/list` 实测），
  49 工具那版（`44f957d`，DG832 并入时）也是 0 缺——**那 12 个来自已退役的 DG832 独立
  服务器**（`instrument_*` 命名，13 工具里仅 `instrument_discover` 有 docstring；
  代码在 `D:\ChatWorkspacerchive\DG832使用_archived_20260915\...\mcp_server.py`）。
  37（旧 instruments 快照）+ 12 = 49 与观察吻合。
  - 现场还有 **12 个跑在退役路径上的进程**（`~/.agents/skills/dg832-control/scripts/mcp_server.py`），
    其中 PID 49548（2026-09-14 13:45 启动，早于 09-15 的转发 shim）仍跑着**旧代码**——
    它给客户端的正是那 12 个无描述工具。处置：杀老进程 + 客户端重连（shim 已转发到统一服务器）。
  - 加固：`server.py` 启动自检增报"工具数 + description 缺口"；新增协议级测试
    `TEST_SCRIPTS/common/verify_mcp_tools_meta.py`（§1-§5，含 stdout 纯净性），全 PASS。

- 2026-09-16（同日，MHO 实机尝试 + 发现层修复）：用户说 MHO 在线，让我改偏置/档位复现顶轨。
  **实机不可达**，排查结论（全部实测）：
  - `192.168.1.55` 的 5555/80/111 全超时；本机仪器网卡是 `192.168.1.100/**16**`，
    `local_cidrs()` 只按 /24 扫 → 只覆盖 `192.168.1.0/24`；
  - 遂手工扫干净：**192.168.0.0/16 全段**在 5555 / 80 / 111 / 4880 / 5025 五个端口上
    **零主机应答**（ICMP 扫描 `192.168.1.0/24` 也只有本机；ARP 无邻居）；
  - VISA 只枚举到 5 个串口（无 USB/LAN 仪器）；USB 侧无 RIGOL MHO，
    只有 **DG832 的 USBTMC 节点处于 `Error` 状态**（`USB\VID_1AB1&PID_0643\DG8A26510320…`）；
  - → 结论：**仪器网络当前不可达**（网口 link 是 Up/1Gbps，但段内没有 IP 主机），
    需要现场核对面板 `Utility→I/O→LAN` 的 IP 与网线。
  - 修复（本次顺带，均为实机踩到）：`local_cidrs()` 改用**接口真实掩码**
    （`max_prefix` 兜底 /16）；`probe_open_ports()` **内部分块**（一次 gather 6.5 万协程会
    MemoryError）；`scan_cidr` 宽网段（>1024 地址）预筛改走**分块异步**（/16 × 4 端口实测
    **166s**，原线程版约 5 分钟）。新增离线回归 `verify_discovery_local.py`（13 项全 PASS）；
    内核补 `snap_down`（与 `snap_up` 对称，供缩小档位用）。
  - 待办：**MHO 可达后跑** `TEST_SCRIPTS/mho/verify_rails_live.py`（已写好：自动挑非触发源
    通道 → 逐档缩小 scale 逼出顶轨 → 断言 `rails()` 分顶/底 + `fit_channel` 救回 →
    `finally` 恢复原设定并留痕 `TEST_DATA/mho/rails_live_*.json`）。

- 2026-09-16（同日，**MHO 真机复现顶轨** + 三条实机更正）：MHO 插上网线后可达
  （`192.168.1.55`，`RIGOL TECHNOLOGIES,MHO984D,MHO9B282003181,00.01.00`）。
  用 `TEST_SCRIPTS/mho/verify_rails_live.py --ch 3` 在 CH3（3.29 V 直流 + 0.07 V 纹波，
  非触发源）复现并验证：
  - ① 把窗口上沿对准 VMAX → `rails()` 报 `edges_touching=['top']`（顶轨贴边）**PASS**；
  - ② 再把上沿压进信号里 → 设备把**极值与顶/底轨全部回 9.9E37**（且 VAVG 仍给
    2.4855 V 的"看着合理的假值"，真实 3.29 V——**文档 §2.7 的现象实测确认**）→
    `rails()` 新增 `unreadable=True` + "不要采信 VAVG" 提示，`diagnose` 报 `off_screen`；
  - ③ `fit_channel` 自动救回（读数有效、margin_ok）；④ `finally` 恢复原设定（CH3 2.0/−3.5）。
  **实机更正三条**（都影响工具语义，已改进内核并写回文档）：
  1. **偏置量程随档位变**（此前记的"±20 V 与档位无关"只在 scale 0.5/2 上测过）：
     写 −1000 V 探真值 → 0.05→±1 V、0.1/0.2→±10 V、0.5/1/2→±20 V、5/10→**±100 V**
     （写 ±50 回读 ±50 只说明"≥50"，是**假上限**）。→ `fit_channel` 遇钳制改为
     **闭环抬档**；`configure_channel` 的 reasons 写明阶梯。
  2. **抬档必须保持窗口中心**：只写 SCALe 时设备按比例放大 offset（保持波形屏幕位置），
     窗口会随档位"漂走"——实机把中心 1 V/信号 3.3 V 一路抬到 10 V/div 反而跑到中心
     100 V、误报"超出可测范围"。→ 搜索循环抬档后把偏置写回原值。
  3. **极值不可测 ≠ 钳制假值**（对"直流+小纹波"）：顶端出窗时 VMAX/VMIN 直接失效，
     而 VAVG 仍"有值"。→ `rails()/diagnose` 支持**单端出窗**（`top_out_of_window`）与
     **两端都不可测**（`unreadable`）两种明确状态。
  另新增 `measure_retry()`：现场 §2.9 的"偶发无有效值、重读即正常"实测复现（恢复设定后
  第一读即 9.9E37），fit/rails 的读数改走重试。
  回归：`verify_rigol_scope_semantics` **42 项全 PASS**（新增阶梯/抬档保持中心/单端出窗/
  重试等 12 项）；离线六套件 + 两套审计（§A-D 0 / MISS 29 基线）复跑全绿。
  留痕：`TEST_DATA/mho/rails_live_20260916_155042.json`（含 before/plan/trace/恢复记录）。

- 2026-09-16（同日，**远端引入设计复核**）：对 `674038b`（单 worker 异步执行器）、`f55187b`
  （多网段发现）、`0e6222f`（串口子进程隔离）、`7e28880`（隐私收敛）做设计层审查，
  结论"方向对，三处实现有缺陷"，全部修掉并加回归：
  - **D1（设计缺陷，已修）**：执行器用 `concurrent.futures.ThreadPoolExecutor`，其 worker
    是非 daemon 且 `concurrent.futures.thread` 的 atexit 会 join 它们 → **驱动挂起时进程
    退不出来**（离线实验：提交 sleep(600) 后 12s 不退；daemon 同场景 2s 秒退），
    "重启 MCP 服务"这条恢复路径因此失效。改为 **daemon worker + queue**，语义逐条保留
    （单线程封闭/BUSY 闸门/超时不取消 worker/结果与异常原样回填/_busy 只在事件循环写）。
    回归：`TEST_SCRIPTS/common/verify_executor_exit.py`（11 项，含子进程退出证明）；另做
    真机端到端 `mho_status` 调用（走新执行器，返回 CH3 2.0/−3.5 ✓）。
  - **D2（实现不一致，已修）**：`scan_cidr` 识别阶段每台候选各自 `ResourceManager()`
    ——正是 `identify_lan` 文档写明"会随机抛 VI_ERROR_INV_OBJECT 冲垮整轮扫描"的写法
    （`instr_discover` 早已改共享单例，库路径漏了）。改走 `identify_lan_all`（共享 RM）
    + 把预筛开放端口传下去定向协议。实机验证：扫 /24 命中 MHO ✓。
  - **D3（成本取舍，已修）**：`local_cidrs` 改真实掩码后直接扫 /16 要 ~166s，而仪器通常在
    同 /24。新增 `local_scan_segments()`：**由窄到宽**（近邻 /24 → 真实网段）。
    实测 `find_device('MHO', allow_scan=True)` 从"扫不全/166s"变为 **8.3s 命中**。
  - 复核确认合理的部分：单 worker 线程封闭与 BUSY 闸门语义、`device_tool` 返回原同步函数
    （模块内互调不会自我死锁、schema 由 `_preserve_signature` 保持）、首次工具调用时才
    重定向 stdout（时序论证成立）、串口子进程隔离（含挂起口缓存 + instr_discover 清缓存）、
    隐私收敛（序列号 0 命中 + 夹具打桩）。AST 核对：56 处 `_call(` 全部经 `@device_tool`
    工具体（含 `_guarded_call` 薄封装）→ 不存在"事件循环里跑设备 I/O"的路径。
  - 设计文档 `docs/gpt_qa/20260915-mcp-async-refactor.md` 追加"复核更正"节（不改原文），
    skill 补"为什么'重启 MCP'确实有效"的依据。回归：七套离线套件 + 两套审计全绿。

- 2026-09-17：**VISA 并发假设的实机论证**（MHO984D over LAN + DG832 over USB-TMC；
  脚本 `TEST_SCRIPTS/common/verify_visa_concurrency.py`，只读查询；报告
  `docs/visa_concurrency_20260917.md`）。结论：
  - 共享单 RM + 并发访问**不同**设备 → **安全**（300 次并发 0 错误、真并行）；
  - 同一资源并发 open 有**上限≈15~16**（其余 `VI_ERROR_ALLOC`）；
  - "每线程各自 RM 会抛 VI_ERROR_INV_OBJECT"（对端现场说法）**本机未复现**（8/8 成功）；
  - **同一设备两会话并发 → 静默串台**：USB-TMC 实测串台 100~115 次 + 丢响应约一半、
    3 次复现；MHO 的 VXI-11/raw 两会话同样串台；串行化后 **0 串台 0 错误**（H5）；
  - 并发超时未读会把 MHO 的 **VXI-11 通道搞成稳定滞后一条且不自愈**（新进程/新会话/
    `clear()` 都解不开）→ 按仓库既有做法**换协议**恢复（`resolve("mho")` 已切 raw socket，
    实测 raw 对齐、截图/波形/测量全可用）；VXI-11 需设备侧重置 LAN 或重启；
  - 护栏有效：把地址指回错位通道，`_verify_idn` 以"地址校验失败（`*IDN?`='2.000000E+00'）"
    **拒绝操作**，不会返回错值（`error_type=connection`）。
  - 顺带修掉一个真 bug：**raw socket 无消息分帧**，整屏 PNG（97 471 字节）单次 `read_raw`
    只拿到 6 字节 → 新增 `VisaClient.query_block()`（按 TMC 头长度读满）+ `screenshot()` 改用它。
  - 纪律已写进 `AGENTS.md` 铁律 17/18 与 skill（含症状与恢复步骤）。

- 2026-09-17（同日，**跨进程会话咨询锁** + 善后）：按用户口径实施"同设备判重**按 VISA 地址**、
  跨接口（inst0/5555）只额外告警（未验证）"的咨询锁 `common/session_lock.py`：
  - 每次设备工具调用刷新自己的锁文件（**文件名带 PID**——踩坑回归：按地址命名会让两个进程
    互相覆盖/漏报）；活跃判据 = PID 存在（Windows 用 `OpenProcess`；**绝不能用
    `os.kill(pid,0)`**，那在 Windows 上等于 TerminateProcess）+ 时间戳新鲜（TTL 180s）；
  - 同地址占用 → 工具返回体加 `warnings`（只告警不阻塞）；同一台设备另一接口 → "跨接口
    并发未验证"告警；进程退出 atexit 释放；
  - 回归：`verify_session_lock.py`（离线 15 项）+ `verify_session_lock_live.py`
    （实机 DG832：A 在用时 B 拿到含 PID 的告警、A 退出后告警消失，全 PASS）。
  - 善后如实记录：本轮并发风暴把 MHO 的响应流打成**持续错位**（VXI-11 与 raw 都中招，
    实验后 raw 实测 **0/20 对齐**）→ 需面板重置 LAN 或重启示波器；为此实验脚本把
    "同资源并发（T1b/T2b）"与"LAN 两会话（T3）"都改为显式开启（`--same-res-storm` /
    `--lan-two-session`），默认只跑安全子集 + 结尾对齐核查；并新增
    `RigolScope._float()`（数值项拿到非数值响应 → "响应错位 + 处置建议"）与
    `align_session()`（`*IDN?` 判对齐 + 尝试排干）。DG832 的 USBTMC 节点当日两次掉进
    PnP Error，均用 `common/usb_reset.py --kind dg --allow-reset --verify-idn` 秒级恢复。
- 2026-09-22（MCP stdio 编码：GBK → UTF-8；启动自检改 ASCII）：用户报 DSH 控制台出现
  `xterm.js: Parsing error: {... code: 65533 ...}`（U+FFFD＝非 UTF-8 字节被按 UTF-8 解码），
  且 traceback 行首被吃掉半截（现场片段 `ackages\anyio\_backends\_asyncio.py`）。根因**不在
  仪器侧**：stdio 传输下 MCP 子进程的 stderr 是 **pipe**，Python 在中文 Windows 上按
  **GBK(cp936)** 写中文日志，而客户端按 **UTF-8** 读 → 替换字符，xterm 解析器随后报
  `code: 65533`。
  - 编码矩阵实测（本机 `pythoncore-3.14-64`，stderr = pipe）：默认 `gbk`｜`PYTHONUTF8=1` →
    `utf-8`｜`sys.stderr.reconfigure(encoding="utf-8")` → `utf-8`。复现：`"启动自检"` 在默认
    口径下写出 `c6f4b6afd7d4bcec`（GBK 字节），按 UTF-8 解码全成替换字符。
  - 修法（三条都落地）：① `server.py` **模块级** `sys.stderr.reconfigure(encoding="utf-8",
    errors="backslashreplace")`——治本、不依赖客户端；只改现有 wrapper 的编码，不动对象、
    不动 `sys.stdout.buffer`，不影响 `mcp.run()` 自建的协议流；② 启动自检与失败兜底**改纯
    ASCII 英文**（异常用 `ascii(e)` 转义），标记词 `启动自检` → `startup self-check`；
    ③ DSH 侧（用户配置，不在本仓）`~/.dsh/profiles/web/cordis.patch.yml` 的 `mcp-instrument`
    加 `env: {PYTHONUTF8: '1'}`——**必须重启 DSH** 才生效（HMR 不重启已有 MCP 子进程）。
  - 回归：`verify_mcp_tools_meta.py` §5 **不需要改**——上游已用 ASCII 锚点（`[instrumentControl]`
    + 正则 `description` + 空白 + 数字），只要自检行是 ASCII 且保留 `description <n>` 空格写法即可。
    字节级复验——显式移除 `PYTHONUTF8`/`PYTHONIOENCODING`（回到默认 GBK 口径）抓 server stderr =
    **401 字节 / 非 ASCII 0 个**；`py_compile` 通过；patch YAML `yaml.compose` 语法 OK。
  - 留痕：`TEST_DATA/common/mcp_stderr_encoding_20260922_183137.json`（编码矩阵 + 逐行 stderr）。
  - 对齐提醒：自检行措辞变了，按中文字面 grep 的脚本要跟改；本仓两个验收脚本（common 与 ks3458a）
    都已改用 ASCII 锚点、无需改动，但都要求 `description <n>` 空格写法（勿写成 `description=<n>`）。

- 2026-09-23（合并上游 3458A 批次 + 重放本次编码改动）：`git fetch` 到 `d9aecbd`（12 个提交：
  3458A/82357B 库与工具、SKILL/docs、工具数 57→68），本地 0 独有提交 → `main` **fast-forward**；
  编码改动从安全分支 `mcp-stderr-utf8`(be1060c) **重放**到新版 `server.py`（上游把该文件与
  `verify_mcp_tools_meta.py` 存成了 **CRLF**，故提交用 `core.autocrlf=false` 保持 CRLF，避免
  全文件行尾抖动）。重放后 `verify_mcp_tools_meta.py` **全部 PASS**（68 工具、`description gap = 0`、
  stdout 纯净），`py_compile` 通过；自检行文案按上游断言保留 `description <n>` 空格写法。
