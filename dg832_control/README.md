# dg832_control — RIGOL DG800 系列信号源（DG832 基准）

本目录是 **DG832 控制的唯一维护点**（2026-09-15 合并）。

| 文件 | 说明 |
|---|---|
| `dg832.py` | 驱动器本体（原样迁入，未改一行；含保护联锁 / DC 快照 / 扫频 / 频率计 / CLI） |
| `__init__.py` | 对外导出（`DG832` / `discover` / 异常类 / `MODEL_REGISTRY`） |
| `docs/DG832使用笔记.md` | 使用笔记（型号对照、快速上手、踩坑记录） |
| `docs/00_快速指南.txt`、`01_用户手册.txt`、`02_编程手册.txt` | 原厂手册文本（**命令出处的审计参照物**：`02_编程手册.txt`） |

## 迁移说明

2026-09-15 合并前，同一份 `dg832.py` 有三处**逐字节相同**的副本（散落在旧工作区与
两个 skill 目录），外加两个 MCP 包装（`mcp_dg832/server.py` 与 skill 内的
`mcp_server.py`）。现统一为：**库** = 本目录 `dg832.py`；**MCP** =
`mcp_instruments/server.py` 的 `dg_*` 工具。旧副本、旧 skill 与旧 MCP 包装均已删除。

## 与仓库其它库的差异（有意保留）

- **模型注册表**：DG800 全系列同命令集（DG811/812/821/822/831/832），故工具带 `model`
  参数（默认 `DG832`，未登记型号返回 `model_unsupported`）。
- **保护联锁（protect）**：设置幅度/偏移、打开输出前必须已开启有效电压保护
  （`state=True` 且 `high>low`），否则 `protect_required`；这是本库自带的**硬件防超压**设计，
  合并时原样保留（比仓库其它信号源的 `expect_load` 声明更强）。
- **DC 切换**：`set_dc_only()` 返回切换前快照，切回时要求**显式**传参，不做隐式恢复。

## 台面基线（原工作区 `reset_baseline.py` 记录的现场状态）

收工想把设备还原成"接手时那样"，就按这个来（原脚本已随工作区归档，值抄在这里）：

| 通道 | 波形 | 参数 | 电压保护 |
|---|---|---|---|
| CH1 | DC | 电平 2 V | **ON** |
| CH2 | SIN | 1 kHz / 5 Vpp | OFF |

对应命令（先开保护再设波形，库内联锁）：

```python
gen.set_voltage_limit(1, state=True)      # CH1 保护 ON
gen.set_wave(1, "dc", offset=2.0)         # DC 电平走 offset
gen.set_wave(2, "sine", 1000, 5.0)        # 5 Vpp（保护 OFF 时设幅度会被拒 → 先开再关）
gen.set_voltage_limit(2, high=5, low=-5, state=True)
gen.set_wave(2, "sine", 1000, 5.0)
gen.set_voltage_limit(2, state=False)     # CH2 保护 OFF
```

现成的写路径回归（会改设定并自动恢复）：`TEST_SCRIPTS/dg832/test_dg832_write_matrix.py --allow-write`。

### 写回顺序（2026-09-15 真机实测教训）

**先把保护窗口放回/放宽，再写波形参数**，最后才按备份收窄窗口。顺序反了会被设备的
"offset ± amp/2 必须落在 [low,high] 内"规则**钳制**：实测恢复 CH2 时先写
`:SOUR2:APPL:SQU 5e4,3.3,1.65,0`（当时保护窗口还是测试留下的 `high=3`），
设备把 offset 从 1.65 钳成 **1.35**（= 3 − 3.3/2），而只比"波形名"的断言会看着像成功。
`verify_dg832_edge_cases.py` 已按此顺序实现，并把断言改成**全参数比对**（含容差）。

## 安全约定（与 AGENTS.md 一致）

- 输出开关（MCP `dg_output`）**需 `confirm=True`**：关断同样可能打断正在进行的测试；
- 改变输出状态前先 `dg_status` 查通道配置/输出开关/负载；
- 脚本必须 `try/finally` 恢复被改设定并关闭输出；留痕到 `TEST_DATA/dg832/`。
