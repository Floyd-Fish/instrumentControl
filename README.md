# instrumentControl

**给 AI/Agent 用的仪器控制 MCP**：把实验室仪器（示波器 ×3 / 信号源 ×2 / 万用表 / 电源 / 校准器）
统一封装为 VISA-SCPI 驱动库，再由 `mcp_instruments/` 暴露成 MCP 工具，
让 AI 能安全地发现设备、查询状态、自动定标、测量、截屏、上下电。

设计前提：**AI 会看手册、会截图，也会犯错**。所以本项目把"命令不许猜、状态要回读、
危险动作要装门"做成**代码里的硬约束**，而不是文档里的建议。

```
AI 客户端（DSH / Codex / Claude / opencode …，经 MCP stdio）
        │
        ▼
mcp_instruments/server.py ─── 68 工具（64 设备专用 + 3 通用护栏 + 1 故障兜底），无状态连接+全局锁串行化
        │
        ├─▶ sds_control       Siglent SDS800X HD 示波器（波形/截图/测量/触发诊断/auto_scale）
        ├─▶ sdg_control       Siglent SDG2000X 信号源（BSWV 键值对）
        ├─▶ keysight_3446x    Keysight 34465A 万用表（CONF/MEAS/NPLC）
        ├─▶ dho_control       RIGOL DHO800/900 示波器
        ├─▶ rigol_scope       共享内核：DHO800/900 与 MHO900 命令集 97% 重合，一份实现
        ├─▶ mho_control       RIGOL MHO900 系列示波器（MHO934/954/984；截图走原生 PNG）
        ├─▶ dg832_control     RIGOL DG800 系列信号源（DG832 基准；保护联锁 + DC 快照 + 扫频）
        ├─▶ dh1766_control    DH1766 三路可编程电源（唯一 pip 可安装，自带手册/经验文档）
        └─▶ emoe_control      Emoe 校准器（骨架：仅发现 + `*IDN?`）
                 ▲
        common/ ─┴─ 统一发现 find_device（resource → hosts → VISA 列表 → CIDR 扫描）+ VisaClient
```

## 三条设计主线

1. **命令零猜测**：每条 SCPI 都对照手册提取版落码；新增命令必过
   `TEST_SCRIPTS/common/audit_all_commands.py` 审计。写操作固定三步
   ——写入 → 查 `SYST:ERR?` → 回读比对（含容差比较与短格式/单位后缀兼容）。
2. **安全门写进代码**（不只是文档约定）：
   - 复位/存储覆写类命令零暴露（MCP 通用写黑名单拦截 `*RST/*SAV/*RCL/:SYST:RES|FACT|PRES`）；
   - 远程锁定类命令禁止（`SYST:REM/SYST:RWL/SYST:LOCK`）——面板要留给现场操作；
   - 输出类操作必须声明当前拓扑/负载并通过校验：
     `sdg_output(..., expect_load)`、`psu_output(..., expect_mode)`、`psu_power_cycle(..., confirm)`；
   - 电源切换输出模式前强制"输出全关"（继电器联动，库内无条件拦截）；
   - 新设备**零代码接入**：`instr_query` / `instr_write` 通用护栏（写前 drain、写后查错、
     自动回读、审计落盘、离线资源硬超时看门狗）。
3. **实测留痕与可回溯**：所有实测输出落 `TEST_DATA/<device>/`（时间戳命名），
   测试脚本放 `TEST_SCRIPTS/<device>/`，注入/回归/审计均有留痕文件。

## 快速开始

```bash
pip install -r requirements.txt          # pyvisa（+ 运行 MCP 服务器需 mcp 包）
pip install -e ./dh1766_control          # 电源库走 src 布局，可独立安装
python mcp_instruments/server.py         # 启动 MCP 服务器（stdio）
```

MCP 注册（示例，路径按需替换）：

```jsonc
// DSH: ~/.dsh/cordis.patch.yml 的 insert 列表；zcode: ~/.zcode/cli/config.json
{ "serverName": "instruments", "transport": "stdio",
  "command": "C:/Users/<you>/AppData/Local/Programs/Python/Python311/python.exe",
  "args": ["<仓库根>/mcp_instruments/server.py"] }
```

> Windows 下必须用 python **全路径**（WindowsApps 别名的 python 在部分客户端 spawn 时失败）。
> AI 侧的使用指引见 skill `instrument-mcp`（工具选择决策树/参数语义/安全门/工作流），
> 权威源在 `skills/instrument-mcp/SKILL.md`，用 `python scripts/install_skills.py`
> 分发到 AI 工具的 skill 根（见文末「Skill 分发」）。

## 目录结构

| 目录 | 说明 |
|---|---|
| `common/` | VISA 客户端 `VisaClient` + 统一发现 `find_device` + **地址解析 `resolve`**（`resolver.py`，不写死 IP） |
| `mcp_instruments/` | MCP 服务器（`server.py`）+ 工具清单（`README.md`） |
| `skills/` | **AI skill 的权威源**（`skills/<name>/SKILL.md`），由 `scripts/` 分发到各 AI 工具的 skill 根 |
| `scripts/` | 部署脚本（`install_skills.py`：skill 安装/检查/卸载） |
| `dh1766_control/` | 电源库（可 pip 安装；手册提取/命令速查/经验总结在 `docs/`） |
| `dho_control/`、`sds_control/`、`sdg_control/`、`keysight_3446x/`、`emoe_control/` | 各设备库 + 手册提取 |
| `TEST_SCRIPTS/` | 实测/验证脚本，按设备分目录 |
| `TEST_DATA/` | 实测留痕（JSON/CSV/PNG，时间戳命名） |
| `docs/` | 使用手册、命令审计报告、实测记录、设计文档 |
| `dg832_control/` | DG832 信号源库（2026-09-15 由嵌套仓库并入本仓；手册/笔记在 `docs/`） |
| `archive/` | 历史版本归档（旧版驱动，可回溯） |

## 设备与发现入口

**地址不是固定资产**：仪器 IP 随 DHCP/换网段变化、USB 换口会换资源串、串口 ASRL 编号会漂移。
所以这里只列"怎么找到它"，**不列地址**——地址由解析层在运行时确定，任何环节都不写死。

| 设备 | 库 | 发现入口（库函数 · 解析层 · MCP 工具前缀） |
|---|---|---|
| DH1766A-1 三路电源 | `dh1766_control` | `find_dh1766()` · `resolve("psu")` · `psu_*` |
| RIGOL DHO924S | `dho_control` | `find_dho()` · `resolve("dho")` · `dho_*` |
| RIGOL MHO984D | `mho_control` | `find_mho()` · `resolve("mho")` · `mho_*` |
| RIGOL DG832 | `dg832_control` | `DG832()`（自动发现） · `resolve("dg")` · `dg_*` |
| Siglent SDS824X HD | `sds_control` | `find_sds()` · `resolve("sds")` · `sds_*` |
| Siglent SDG2122X | `sdg_control` | `find_sdg()` · `resolve("sdg")` · `sdg_*` |
| Keysight 34465A | `keysight_3446x` | `find_dmm()` · `resolve("dmm")` · `dmm_*` |
| HP/Keysight 3458A（八位半，**非 SCPI**） | `keysight_3458a` | `find_3458a()` · `resolve("ks3458a")` · `ks3458a_*` |
| Emoe 校准器（骨架） | `emoe_control` | `instr_discover`（串口 ASRL 编号漂移最频繁，接入前必先发现） |

MCP 专用工具的 `resource` 参数**默认省略**：server 端按
`显式入参 > 环境变量 INSTRUMENT_<KIND>_RES > 本机配置 devices.json > 上次成功缓存 > 自动发现`
解析（实现见 `common/resolver.py`）。

### 配置本机默认地址

这套仪器在你机器上的地址可以写进配置文件（**本机专用，不入库**：
`%LOCALAPPDATA%\instrumentControl\devices.json`）；不配也能用（自动发现 + 缓存）。

**值的形态：一律是「完整 VISA 资源串」**——TCPIP / USB / 串口 / GPIB 都一样，
不用区分传输方式，也不要自己拼 `IP:端口`：

```
TCPIP0::<host>::inst0::INSTR        # VXI-11（SDS/SDG/34465A）
TCPIP0::<host>::5555::SOCKET        # RIGOL raw（DHO）
TCPIP0::<host>::5025::SOCKET        # 大华 raw（DH1766）
USB0::0x0957::0xA007::<serial>::INSTR   # USB TMC（DH1766 也可走这条）
ASRL5::INSTR                        # 串口（编号会漂移）
```

> 为什么不允许手拼：协议/端口/参数因设备而异（DH1766 只认 raw 5025、DHO 只认 5555、
> SDS/SDG/DMM 走 inst0、USB 还需 vid/pid/serial，另有 `visa://<gw>/…` 别名与 mDNS 名），
> 拼错一个字段就是"对未知设备发 SCPI"。**拼接只发生在两处**：发现时的候选生成、
> 以及下面这个"裸 host 规范化"入口——两处都必须经 `*IDN?` 校验才落库。

```bash
python mcp_instruments/config_cli.py show                 # 看当前解析链与来源（不连设备）
python mcp_instruments/config_cli.py set sds "TCPIP0::<host>::inst0::INSTR"   # 完整串：直接写
python mcp_instruments/config_cli.py set sds 192.168.31.220                   # 裸 host：自动探测协议+校验身份后写入规范串
python mcp_instruments/config_cli.py autofill             # 把 instr_discover 的发现结果固化成配置
python mcp_instruments/config_cli.py clear sds            # 删条目 → 回落自动发现
```

换网段/换 USB 口后：先跑一次 `instr_discover`（发现结果会自动回写缓存）；
若地址已被 DHCP 分给别的设备，工具会在连接后**核对 `*IDN?` 并拒绝操作**（不会误发 SCPI）。

## 安全摘要（完整红线见 `AGENTS.md`）

- **输出/信号类操作**（`sdg_output`/`psu_output`/`psu_power_cycle`）开与关都需 `confirm=True`：
  它代表"**已获得关断授权**"（用户本轮明确要求，或明确声明独占使用），不是"我知道要关"。
- **操作电源前先查模式**（`psu_status`/`psu_mode`）：TRAC 下 CH2 跟随 CH1 输出**负压**是正常现象。
- **禁止远程锁定与复位**：`SYST:REM/SYST:RWL/SYST:LOCK`、`*RST/:SYST:RES/:SYST:FACT` 一律不可用。
- **测试脚本必须 `try/finally` 恢复**被改设定并关闭输出。
- 示波器读数超屏会被钳制 → "有无波形/是否削顶"用**截图**判断，不要迷信设备测量值。

## 扩展新设备

完整流程见 skill `instrument-mcp` →「新仪器渐进式接入流程」（五步：发现 →
只读跑通 → 写验证 → 落库+分类 → 防回归）。概要：`instr_devices(full=true)` 定位
资源 → `instr_query`（只读）跑通手册查询 → `instr_write(confirm=True, readback_cmd=...)`
验证写命令 → 命令有了出处后落库（`commands.py` 常量 + 库方法 + 可选 MCP 工具 +
**catalog 风险分类**——未分类服务起不来）→ 跑命令审计器与离线回归。

## 文档导航

| 文档 | 内容 |
|---|---|
| `AGENTS.md` | **Agent 工作规范（铁律/安全红线/排查流程）——操作仪器前必读** |
| `skills/instrument-mcp/SKILL.md` | **MCP 使用指引（权威源）**：工具选择决策树、参数语义、安全门、典型工作流；由 `scripts/install_skills.py` 分发到 `~/.agents/skills/` |
| `mcp_instruments/README.md` | MCP 工具清单与安全约定 |
| `docs/AI_OPERATION_GUIDE.md` | AI 操作手册：各库 API、固件特性、闭环范例 |
| `docs/TEST_RECORDS.md` | **历轮实测记录（时间线）** |
| `docs/ip_hardcode_audit_20260913.md` | 仪器地址硬编码专项审计（结论：地址一律走解析层，勿写死） |
| `docs/command_audit_20260823.md` | SCPI 命令审计报告（零猜测命令结论） |
| `dh1766_control/docs/EXPERIENCE.md` | DH1766 时序/固件差异/上电过渡态等实测经验 |

## Skill 分发（仓库为源，安装到 AI 工具）

`skills/<name>/SKILL.md` 是**唯一权威源**，不再到处维护副本。历史上
`mcp_instruments/SKILL.md` 与 `~/.agents/skills/instrument-mcp/SKILL.md` 是双副本、
靠人工同步，已在本版收敛为单一源。

```bash
python scripts/install_skills.py --check      # 只报告：已链接 / 是旧副本 / 未安装
python scripts/install_skills.py              # 安装（默认建目录链接，改仓库即生效）
python scripts/install_skills.py --copy       # 复制安装（需要独立副本时）
python scripts/install_skills.py --uninstall  # 卸载（只动指向本仓的链接）
```

- **默认目标根** `~/.agents/skills`（DSH / Codex / ZCode 共享的通用 skill 根）；
  用 `--root <dir>` 指定其它根，可重复。
- **默认建目录链接**（Windows junction）而非复制：仓库改完**不需重装**即生效，
  从根上消除"两份内容不一致"；DSH 的 skill watcher 默认跟随符号链接，热更新正常。
- 目标已存在且内容与源不一致时**拒绝覆盖**（保护本地修改）；`--force` 才覆盖，
  且会先完整备份到 `TEST_DATA/common/skill_backup_<name>_<时间戳>/`。
