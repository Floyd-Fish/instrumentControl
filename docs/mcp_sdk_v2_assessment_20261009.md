# MCP Python SDK 1.x vs 2.x 调研（2026-10-09）

> 结论：**维持 `mcp>=1.27,<2` 锁定**。v2 的核心收益（无状态协议、多实例水平扩展、
> serverless 部署）与本仓场景（桌面单例、有状态设备 I/O）**正交**；1.x 官方维护
> （关键修复+安全补丁，无 EOL 公告）。迁移留待触发条件，见文末。

## 一、v2 是什么（2026-07-28 发布，协议 + SDK 双大版本）

- **协议层**（2026-07-28 spec）：MCP 从双向有状态会话变为**无状态请求/响应**——
  单个自描述 HTTP POST，不再需要握手与 sticky session；新增 `server/discover`、
  `subscriptions/listen`、MRTR（多轮往返，无状态 elicitation）；Tasks 移为 extension。
- **SDK 层**：`FastMCP`→`MCPServer`（模块 `mcp.server.fastmcp`→`mcp.server.mcpserver`）；
  wire 类型拆成独立 `mcp-types`（snake_case 字段）；传输配置移出构造（`port=` 参数
  变 TypeError，配置进 `run()`）；一个 `Client` 对象替代三层叠；同步 handler 自动跑
  worker 线程。
- 实测数据（Manufact，托管数千 MCP 服务器）：包体积 -83%、速度 +25%。

## 二、本仓逐条对照（为什么"更好"落不到我们头上）

| v2 卖点 | 本仓实况 | 判定 |
|---|---|---|
| 无状态、多实例/serverless | 刻意单例（一仪器一机一进程）；设备 I/O 天然有状态（BUSY/设备锁/VISA 会话）| 负相关 |
| 同步工具自动线程化 | wrapper 是 async def（`await _executor().run()`），v2 的 sync-thread 不触发；自建执行器语义更精细（BUSY 立即返回、超时不杀 worker）| 0 收益、无冲突 |
| Client 简化 | 客户端脚本三层写法略啰嗦 | 微收益 |
| -83% 体积 / +25% 速度 | 常驻 72MB、瓶颈在仪器物理响应（秒级）| 无感 |
| MRTR elicitation | confirm 走参数不走服务器侧征询 | 暂无用途（未来交互式授权可用）|

一句话：**v2 为"云上无状态多实例"重构；本仓是"桌面上有状态单例"——卖点与场景正交。**

## 三、1.x 生命周期

- 官方定位：maintenance（关键修复 + 安全补丁，不加功能）；原话 "v1.x is not going anywhere"。
- 1.30.0（2026-09）仍在出（Py3.14 支持、会话过期、OAuth 加固）——**安全线是活的**。
- 官方给不迁移者的处方＝本仓现状：`mcp>=1.27,<2` 上界。
- **无 EOL 日期公告**。

## 四、若迁，代价清单（预研）

1. 机械（约半天）：import 改名；`_run_server()` 的 `mcp.settings.host/port` 赋值 →
   `run()` 传输参数重写；snake_case 字段适配。
2. **最大不确定性**：`mcp._tool_manager` / `Tool.from_function` 属内部 API，v2 无
   等价承诺——registry parity 的 schema 逐字节防漂移守卫可能要换地基重做。
3. 六个 verify 脚本 + 客户端脚本（三层叠→`Client`）+ compact_tools 全要动。
4. 全量回归 + 本机/远程双机部署同步。**估算 1~2 天**，风险集中第 2 条。

## 五、迁移触发条件（写明，避免反复纠结）

1. 1.x 停止安全维护（官方公告 EOL）；
2. 真需要 MRTR（如 confirm 改服务器侧交互征询）；
3. 客户端生态全面 2.x-only（目前远未发生——v2 服务器兼容旧客户端，旧服务器也仍被普遍支持）。

可选预备：分支上做半天 spike 验证 `_tool_manager` 的 v2 等价物，消掉最大不确定性（不合并）。

## 关联

- 版本锁定：`requirements.txt`（`mcp>=1.27,<2`，实测 1.27.0 本机 / 1.30.0 远程）
- 踩坑记录：`docs/TEST_RECORDS.md` 2026-10-09（远程部署时 pip 默认拉 2.3.0 翻车）
- 官方文档：py.sdk.modelcontextprotocol.io（/whats-new、/deprecated、migration guide）
