# GPT 讨论：仪器控制 MCP 网关化架构（2026-09-23）

<!-- chatgpt-discuss: session=instrument-gateway-arch conv=https://chatgpt.com/c/6ab37be8-9a44-83e9-a274-4e487364b947 updated=2026-09-23 16:13:36 rounds=4 -->
- 会话: instrument-gateway-arch | 链接: https://chatgpt.com/c/6ab37be8-9a44-83e9-a274-4e487364b947

## Q1 2026-09-23 15:16:34
**Q:** 背景：我在维护一个仪器控制项目（Python，控制 RIGOL/Keysight 等真实台式仪器）。现状分两层：

【库层】已经是干净的：每个设备一个独立 Python 包（sds_control 示波器、sdg_control 信号源、dg832_control 信号源、keysight_3446x 与 3458a 万用表、dh1766_control 电源、dho_control/mho_control 示波器、rigol_scope），共用 common/ 发现层与地址解析层（不写死 IP）。这些库可以直接 import 使用，已用于脚本。

【MCP 层】一个 server.py（约 3400 行，FastMCP），用 @device_tool() 装饰器把 57 个函数注册成 MCP 工具，模型侧名字是 mcp__instrument__<name>，前缀为 dg_ / sds_ / sdg_ / dmm_ / dho_ / mho_ / psu_ / ks3458a_ / instr_。每个工具的描述里内联了大量排障知识与实测结论（设备行为、强制流程、安全门语义）。安全护栏都在 server.py 里：复位类命令黑名单（confirm 也不放行）、输出/关机类需显式 confirm、写后回读验证、错误队列排空、审计落盘。

【消费方】DSH、Codex、Claude、opencode、zcode 等多个客户端，都走 MCP stdio。

【实测数据】这 57 个工具的定义（名称+描述+JSON Schema）在每次请求里占约 13.7k token，占该 harness 静态上下文注入总量（约 4 万 token）的 44%；而多数会话根本不碰仪器。

【我的猜想】想改成"按需暴露"，倾向不再堆一堆 MCP 工具。

三个候选方向：
方案A（网关）：只注册一个或极少数"申请入口"工具，例如 instrument(action, params)，真正能力由服务端自己实现 API，模型按需查询能力目录后再调用。
方案B（代码执行）：不做 MCP 工具堆，改为把库 + 一个稳定 CLI 暴露给 agent，让 agent 自己写 Python 脚本调用。工具定义几乎为零，且天然支持循环、扫频、批量采集这类组合操作。
方案C（混合）：保留少量高频工具（discover/status/query 等），其余走网关或脚本。

请帮我分析：

1. 这三种模式各自的取舍？特别是对"模型写脚本组合调用"这类场景，哪个更合适、为什么？

2. 单一网关工具的代价是什么（schema 变弱、模型必须先查能力目录、参数错误更难被发现）？业界有没有成熟的 progressive disclosure / 工具懒加载实践可以参考？

3. 安全护栏怎么在网关模式或脚本模式下保持？如果走脚本模式，模型可以绕过 MCP 层直接对仪器发任意 SCPI 命令，这个风险边界该怎么设计（例如护栏下沉到库层）？

4. 多客户端兼容性：有的客户端支持用配置过滤工具（glob 排除），有的不支持。这对方案选择有什么影响？

5. 如果要迁移，有没有推荐的分阶段路径（既降低风险，又不破坏现有可用性）？

请给出明确的推荐方案与理由，不要罗列所有可能性而不下结论。


**A:**
结论先给：

**推荐采用 C，但具体形态应是“轻量 MCP Broker + 按需能力发现 + 受控 Code Mode”，不建议长期保留一批高频设备工具。**

默认模型侧只看到约 4 到 5 个稳定工具：

```text
instr_discover()       # 当前有哪些仪器
instr_search(query)    # 搜能力
instr_describe(ids)    # 取完整 schema、语义、安全说明、示例
instr_call(op, args)   # 单次调用
instr_run(code/plan)   # 组合、循环、扫频、批量采集
```

现有 57 个 `dg_* / sds_* / ...` 工具继续保留为 **legacy profile**，用于兼容旧客户端和迁移期。最终业务能力仍然有 57 个甚至未来 200 个，只是不再全部进入模型静态上下文。

其中最关键的一点是：

> **`instr_run` 里的模型代码只能调用受控 instrument API，不能得到任意 Python、socket、pyvisa、subprocess、文件系统和网络能力。**

这实际上结合了 A 的低上下文成本和 B 的组合能力，同时避开纯 A 的弱 schema 与纯 B 最大的安全问题。

---

## 1. A / B / C 怎么选

### A：单一 Gateway

适合解决您当前最显性的 13.7k token 问题，但如果设计成：

```python
instrument(action: str, params: dict)
```

我不建议。

它会把原来的：

```text
57 个强类型 schema
```

压缩成：

```text
1 个几乎无类型的 dict
```

模型对参数名、单位、枚举、互斥参数、设备差异的错误只能等服务器运行时发现。

正确的 Gateway 应该采用：

```text
search → describe → validate → execute
```

`describe("sdg.set_frequency")` 动态返回：

```json
{
  "operation": "sdg.set_frequency",
  "schema": {...},
  "examples": [...],
  "safety": {...},
  "notes": [...]
}
```

执行时服务端仍然拿这个 schema 做严格验证。因此损失的是**静态模型可见 schema**，服务器端类型检查完全可以保留。

这正是目前 progressive disclosure 的主流方向。Anthropic 的 Tool Search 已明确支持 deferred loading，官方建议在工具超过 10 个或定义超过 10k token 时使用；其文档还指出，工具数量达到约 30 到 50 个以后，模型的工具选择准确率会明显受影响。您的 57 个工具和 13.7k token 正好落入这个区间。citeturn372374search0

### B：让 agent 自己写 Python

对于：

```text
for f in frequencies:
    gen.set_frequency(f)
    wait_settle()
    v = dmm.measure()
    results.append((f, v))
```

这种工作，**代码明显比逐次 MCP tool call 更合适**。

原因有四个：

1. 循环、条件、数据处理天然就是程序控制流。
2. 不需要每一次调用都经过一次 LLM inference。
3. 中间的 1000 个采样结果可以留在执行环境里，只把汇总结果送回模型。
4. 可以封装 cleanup、超时、锁、异常处理。

Anthropic 目前也专门提供了 Programmatic Tool Calling，其理由就是循环、条件和数据转换更适合由代码完成。Cloudflare 的 Code Mode 也采用同一模式，模型编写代码协调工具，再只把必要结果送回上下文。citeturn372374search2turn521784search3

所以从**执行模型**看，我赞成 B。

但我不赞成：

```text
agent shell
   ↓
python xxx.py
   ↓
import sds_control / pyvisa / socket
   ↓
真实仪器
```

这让 MCP 中的全部安全控制失去意义。

### C：我推荐的版本

应该是：

```text
                    ┌─ instr_call()
Agent → compact MCP ┤
                    └─ instr_run(code)
                              │
                       restricted runtime
                              │
                    guarded instrument API
                              │
                     policy / audit / lock
                              │
                    existing device libraries
                              │
                         VISA / LAN
```

这样脚本能力和安全边界可以同时保留。

Cloudflare 现在甚至已经正式提供这种 MCP 结构：一个 MCP server 可以只暴露 `code`，或者使用 `search + execute`；其 Cloudflare API MCP 用两个工具覆盖 2500 多个 API endpoint。citeturn521784search4turn521784search8

所以这已经不属于很实验性的架构猜想，progressive discovery + programmatic composition 已经形成明确实践。

---

# 2. 单一 Gateway 真正的问题

您列的三个代价都成立，但严重程度不同。

### 2.1 最大的问题是 schema discoverability

例如现在模型直接看到：

```python
sdg_set_frequency(
    channel: int,
    frequency_hz: float,
    ...
)
```

Gateway 后可能只看到：

```python
instr_call(
    operation: str,
    arguments: object
)
```

所以 `operation` 拼错、单位搞错、参数缺失不会被 MCP JSON Schema 提前拦住。

解决方案是把 schema 变成**按需数据**：

```text
instr_search("set signal generator frequency")
        ↓
sdg.set_frequency
sdg.configure_channel

instr_describe(["sdg.set_frequency"])
        ↓
完整 schema + 示例 + 陷阱 + 安全属性
```

然后模型调用：

```text
instr_call(
    op="sdg.set_frequency",
    args={"channel": 1, "frequency_hz": 10000}
)
```

服务端再次依据 registry 中同一个 schema 校验。

因此我建议建立一个真正的 **Operation Registry**，成为唯一事实源：

```python
Operation(
    id="sdg.set_frequency",
    summary=...,
    schema=...,
    examples=...,
    risk=...,
    implementation=...,
    notes=...,
)
```

然后：

```text
legacy MCP 57 tools
compact MCP
CLI
Code Mode SDK
documentation
```

全部从 registry 派生。

这样不会出现现在 `server.py` 描述、CLI、库 API、安全语义逐渐分叉的问题。

### 2.2 多一次 search 的 latency

确实存在。

但您的场景里：

```text
13.7k 静态 token × 每一轮
```

和：

```text
涉及仪器时多一次 10 到 100 ms 级本地目录查询
```

通常没有可比性。

而且一个会话里发现过：

```text
sdg.set_frequency
dmm.measure_dc_voltage
```

以后可以缓存，不需要反复搜索。

Anthropic 的 Tool Search 默认就是一次找 3 到 5 个相关工具，并自动载入其完整定义。citeturn372374search0

### 2.3 搜索召回错误

这个风险真实存在。

您的命名其实非常适合搜索：

```text
dg_
sds_
sdg_
dmm_
dho_
mho_
psu_
ks3458a_
instr_
```

建议 registry 保留结构化字段：

```text
device_family
operation
aliases
keywords
SCPI concepts
risk
```

例如：

```text
id: sdg.set_frequency
aliases:
  - set frequency
  - frequency
  - freq
  - configure generator frequency
keywords:
  - signal generator
  - SDG
  - source
  - Hz
```

57 项这个规模甚至不需要 embeddings，BM25 / keyword / prefix ranking 已经够用。

---

# 3. 安全护栏放哪里

这里我建议修正一个概念：

> **不要简单地“把 MCP 护栏下沉到设备库”。应当抽出一个 agent 不可绕过的 policy broker。**

分层可以是：

```text
drivers/
    sds_control
    sdg_control
    keysight_3446x
    ...

instrument_runtime/
    registry
    policy
    sessions
    locking
    verification
    audit

frontends/
    mcp_compact
    mcp_legacy
    cli
    code_runtime
```

现有那些干净的设备库可以继续做低层 driver。

所有 **agent-controlled execution path** 必须经过 `instrument_runtime`。

这样不会把：

```text
需要用户确认
Agent 安全语义
审计格式
禁止复位
```

这种上层策略污染到纯设备驱动里。

---

## 一个非常重要的安全问题

如果 agent 可以：

```python
import socket
socket.create_connection(("192.168.x.x", 5025))
send("*RST")
```

或者：

```python
import pyvisa
```

那么您在 Python library 里加多少安全检查都没有用。

**安全边界取决于 agent 是否拥有绕过 broker 的物理能力。**

所以纯方案 B 最大的问题其实不是软件架构，而是 capability containment。

理想部署是：

```text
agent/code sandbox
       │
       │ only IPC
       ▼
instrument broker
       │
       ├─ VISA
       ├─ USBTMC
       └─ TCP/5025
```

并使 agent runtime 本身：

```text
无 instrument VLAN 网络访问
无 USBTMC/VISA 设备访问
无任意 socket
无 subprocess
无任意 import
```

最强的方案甚至是：

```text
Agent PC              Instrument broker
无仪器网段权限   →     唯一能访问仪器 VLAN
```

如果都运行在 Windows 本机，也至少需要进程级 ACL、防火墙或其他 OS 隔离。

单靠：

```python
不要调用 socket
```

没有安全意义。

---

# 4. `instr_run()` 怎么设计

我建议不要让它执行：

```text
任意 Python
```

模型可以写“脚本”，但脚本环境是 capability sandbox。

例如它看到：

```python
scope = instrument.device("scope1")
gen = instrument.device("gen1")

for f in logspace(10, 100000, 100):
    gen.set_frequency(f)
    sleep(0.05)
    v = scope.measure_vpp(1)
    emit(f, v)
```

但环境没有：

```python
open
os
socket
subprocess
ctypes
pyvisa
importlib
eval
exec
```

每次：

```python
gen.set_frequency()
```

仍然经过：

```text
schema validation
policy
instrument lock
write
readback verification
error queue
audit
```

这和 Cloudflare Code Mode 的思想很接近，他们也是让模型生成代码，但工具调用穿过 sandbox 边界，并且 approval policy 与代码执行本身分离。citeturn521784search1turn521784search11

Python 自己做安全 sandbox 很难。我不会建议用：

```python
exec(code, {"__builtins__": ...})
```

作为安全边界。

更可靠的实现包括：

```text
隔离子进程 + OS 权限限制 + broker IPC
WASM
无 host capability 的 JS/QuickJS runtime
专用 DSL
```

从实现成本考虑，我会优先：

```text
受限子进程 + broker RPC
```

同时限制：

```text
最大执行时间
最大循环次数
最大调用次数
最大结果大小
允许访问的 device set
```

---

# 5. 您现有安全规则怎么迁移

我建议 Operation Registry 至少给每个 operation 这些属性：

```python
risk = READ_ONLY | CONFIG | OUTPUT | DESTRUCTIVE
requires_arm = bool
blocked = bool
verify = VERIFY_READBACK | VERIFY_ERROR_QUEUE | ...
idempotent = bool
timeout = ...
```

例如：

```text
scope.measure_vpp
    READ_ONLY

sdg.set_frequency
    CONFIG

sdg.output_on
    OUTPUT
    requires_arm = true

psu.output_on
    OUTPUT
    requires_arm = true

reset / factory reset
    blocked = true
```

现有：

* reset 黑名单
* 输出/关机确认
* write 后 readback
* error queue drain
* audit

全部移动到这个执行层。

MCP 自身已有 `readOnlyHint / destructiveHint / idempotentHint / openWorldHint` 等 tool annotations，但规范明确说明这些只是 hint，不应承担安全保证。citeturn970085search1turn970085search3

因此 annotation 可以继续输出给客户端做 UX，但真正的 gate 必须在 broker。

---

## `confirm=True` 也值得再审一下

如果当前是：

```python
psu_output_on(confirm=True)
```

而 `confirm` 是模型自己能填的参数，它不能构成强用户授权。

模型完全可以自行生成：

```json
{"confirm": true}
```

更稳妥的机制是 **arming capability**：

```text
用户/客户端
   ↓
显式授权：
device=psu1
actions=[output_on]
max_voltage=5V
expires=10min
   ↓
broker 发 arm_id
```

随后模型只能在这个授权范围内执行。

例如：

```text
arm_id = abc123

允许：
psu1 output_on
psu1 voltage <= 5V

拒绝：
psu2 output_on
psu1 12V
*RST
过期后的任何输出动作
```

而且 **arm_id 必须来自 agent 无法自行创建的授权渠道**。

这比一个 boolean `confirm` 强很多。

如果依赖 MCP UI approval，需要考虑客户端兼容性，所以我会把它当增强层，不作为唯一安全机制。

---

# 6. 为什么特别推荐 Code Mode 给扫频和批量采集

例如 1000 点扫频。

传统 tool calling：

```text
LLM
↓ set_frequency
LLM
↓ measure
LLM
↓ set_frequency
LLM
↓ measure
...
```

会产生大量 inference round trip 和 context accumulation。

Code Mode：

```python
for f in points:
    set_frequency(f)
    rows.append(measure())
return summarize(rows)
```

模型只做一次规划。

对仪器还有额外好处：可以把整个 run 放到一个显式 execution scope：

```python
with instrument.run(devices=["sdg1", "dmm1"]) as run:
    ...
```

broker 可以统一做：

```text
设备锁
session reuse
deadline
取消
异常 cleanup
audit run_id
结果文件
```

如果中途超时，可以执行定义好的：

```text
finally:
    safe_cleanup()
```

这比 agent 每次重新决定“下一条工具调用是什么”可靠得多。

---

# 7. 多客户端兼容性直接决定了不要依赖客户端过滤

您的这个判断很关键。

如果架构依赖：

```text
Claude 支持工具过滤
Codex 支持
客户端 X 不支持
```

最后还是必须保留 57 个静态工具给最弱客户端。

所以：

> **client-side filtering 只能作为优化，不能作为解决 13.7k token 的架构基础。**

MCP 协议本身支持工具列表发生变化。当前 2026-07-28 规范下，`tools/list_changed` 通过客户端主动订阅获得；各客户端是否利用动态列表仍属于 client behavior。citeturn970085search2

这意味着我也不建议您做：

```text
开始只有 3 个工具
↓
用户说“示波器”
↓
服务端动态注册 12 个 scope tools
↓
通知客户端 refresh
```

理论上漂亮，跨 DSH、Codex、Claude、opencode、zcode 时很容易出现兼容性差异。

**静态的 4 到 5 个 gateway tools 是跨客户端最稳的最低公分母。**

而且 MCP 最新协议自身也越来越倾向显式状态句柄。2026-07-28 版移除了协议层 session，并建议应用需要状态时自己发显式 handle。citeturn970085search0

所以例如：

```text
run_id
lease_id
arm_id
```

都应该显式传递，不依赖“这个 stdio 会话里服务器应该还记得”。

---

# 8. 我建议的具体 API

我会把最终默认 MCP surface 控制在下面 5 个。

### `instr_discover`

返回：

```text
logical_id
vendor/model
address
capability groups
status
```

不要返回所有完整 operation schema。

### `instr_search`

输入：

```json
{
  "query": "sweep signal generator frequency and measure AC voltage",
  "devices": ["gen1", "dmm1"],
  "limit": 8
}
```

返回：

```text
sdg.set_frequency
sdg.output
dmm.measure_ac_voltage
...
```

附一两行摘要。

### `instr_describe`

一次可以描述多个：

```text
schema
units
examples
behavior notes
known quirks
safety classification
```

您现在内联在 57 个 descriptions 里的排障知识，绝大部分应该搬到这里。

这一步本身就能大幅缩减 token。

### `instr_call`

用于：

```text
读一次电压
设置一次频率
读 IDN
```

保持低延迟。

### `instr_run`

用于：

```text
sweep
loop
batch acquisition
adaptive test
multi-device coordination
```

内部走相同 registry 和 policy。

这 5 个工具的稳定 schema 很容易压到约 1k 到 2k token 量级，和当前 13.7k 相比已经是数量级改善。

---

# 9. 不建议做的三件事

第一，不建议把默认接口做成纯：

```python
instrument(action, params)
```

没有 search/describe，模型使用体验会明显退化。

第二，不建议默认让 agent：

```text
shell → python → import 设备库
```

只要 agent 还能接触 VISA 或仪器网络，MCP 安全层就已经失去边界。

第三，不建议为了“按需暴露”大量依赖 MCP 动态 add/remove tool。协议能表达，客户端生态差异会成为长期兼容负担。

---

# 10. 迁移路径

我会分五阶段，而且第一阶段完全不改变外部行为。

### Phase 1：先做 Operation Registry

把当前：

```text
57 个 decorator
description
schema
implementation
safety
```

抽成单一 registry。

旧的：

```python
@device_tool()
def sdg_set_frequency(...):
```

改成自动从 registry 生成。

**此时 57 个 MCP 名字完全不变。**

这是最重要的一步，因为以后 compact MCP 和 legacy MCP 才不会形成两套实现。

### Phase 2：把安全执行路径抽成 Broker

形成：

```text
Operation Registry
      ↓
Policy / Validator
      ↓
Executor
      ↓
Device libraries
```

用现有 57 个工具跑完整 regression。

此阶段应验证：

```text
blacklist
confirm/arm
readback
error queue
audit
locking
timeouts
```

都不依赖 MCP wrapper。

### Phase 3：增加 compact profile

新增：

```text
instrument-mcp --profile compact
```

暴露那 4 到 5 个工具。

同时：

```text
instrument-mcp --profile legacy
```

继续暴露现在的 57 个名字。

不要让客户端名称决定服务器行为，显式 profile 更可测试。

### Phase 4：加入受限 `instr_run`

先只支持：

```text
纯控制流
数值运算
instrument proxy
```

逐步增加能力。

尤其先做：

```text
sweep
repeat
measure
collect
aggregate
```

这是收益最大的区域。

### Phase 5：compact 成为默认

通过真实会话比较：

```text
静态 token
总输入 token
tool-selection failure
invalid argument rate
执行 round trip
任务完成率
安全拒绝次数
平均仪器调用数量
```

稳定后将 compact 作为默认，legacy 保留一个较长兼容周期。

---

# 最终架构判断

我会定成：

```text
                 ┌───────────────────┐
                 │  MCP compact      │
                 │ discover/search   │
Agents ─────────▶│ describe/call/run │
                 └─────────┬─────────┘
                           │
Human scripts/CLI ─────────┤
                           ▼
                   Operation Registry
                           │
                    Policy Broker
                   ┌───────┼────────┐
                   │       │        │
                validate  audit   locking
                   │       │        │
                   └───────┼────────┘
                           ▼
                  existing libraries
                           ▼
                    real instruments
```

迁移期旁边保留：

```text
legacy MCP 57 tools
        │
        └────→ same Operation Registry
```

**所以我的选择是 C，但偏向“Gateway + Code Mode”，而非“保留十几个常用原生工具 + 其他另走一条路”。**

原因可以压缩成四条：

1. **57 工具、13.7k token 已经足以值得 progressive disclosure。**
2. **扫频、批采、条件控制天然适合模型写代码。**
3. **任意 Python 直连仪器会破坏您已经建立的安全边界。**
4. **固定的小 MCP surface 对 DSH、Codex、Claude、opencode、zcode 的兼容性明显优于依赖各客户端自己的懒加载能力。**

还有一个我认为值得趁这次迁移一起修正的点：**把“安全确认”从模型可填写的 `confirm=True` 升级成 broker 侧的 bounded arm/capability，并把设备、动作、参数上限和有效期绑定进去。** 对真实电源、信号源输出这类操作，这会让整个架构的安全语义清楚很多。

## Q2 2026-09-23 16:04:21
**Q:** 上一轮你给的方案 C 我已经实现到阶段 3。先汇报实际落地情况（与你的建议有出入的地方我标出来了），再请你评审阶段 4——**我认为你上一轮的安全论证在本题的部署形态下不成立，请重点评估这一点**。

【已实现（分支 v2dev，全部离线验证，未碰仪器）】

阶段1：Operation Registry
- 新增 instrument_runtime/{registry,catalog}.py。68 个操作用同一个 @device_tool 装饰器登记；canonical id 由工具名首个下划线切分派生（sdg_set_wave → sdg.set_wave）。
- 我**没有**采纳"把 schema 做成 registry 里的显式数据"这一建议，而是从 FastMCP 自己的 Tool.from_function 生成后原样取。理由：手写 schema 等于把 FastMCP 的 schema 规则复制一份，将来必然漂移。
- catalog.py 是 68 项声明式安全分类：risk（read_only=31 / config=28 / output=4 / destructive=5）、requires_confirm（10 个）、raw_scpi（3 个）、verify、note。requires_confirm 取函数签名实测值并与 catalog 复核，不一致直接抛错；**未分类的工具会让服务启动失败**。
- 验收：tools/list 快照 sha256 重构前后完全一致（68 工具 / 123116 字节）。

阶段2：Policy Broker
- 把黑名单(policy.py) / 审计(audit.py) / 回读验证(verify.py) / 设备锁(broker.py) 抽进 instrument_runtime/；server.py 保留原私有名作别名（既有调用点一行未改，净减 73 行）。
- 我按你的分层建议做了，但**没有把执行器（预算/超时/线程封闭）一起搬**——那部分要真实仪器才能验证，留在 server.py。
- broker.py 提供 decide_scpi(cmd, require_query) 作为唯一放行口；设备锁是**同一个对象**。
- 验收：子进程断言 import broker 不牵入 mcp/fastmcp/pyvisa；server 的私有名与 runtime 是同一函数对象；decide_scpi 与迁移前参考表达式在 38 条语料逐条等价；仓库既有的护栏穷举审计（七套手册）0 误伤 / 0 拒绝。

阶段3：compact profile
- server 支持 --profile=legacy|compact（默认 legacy）。compact 暴露 4 个工具：instr_devices / instr_search / instr_describe / instr_call。
- 实测（真实 MCP 线格式、两者同口径）：legacy 68 工具 59677 字符 ≈19.9k token；compact 4 工具 2674 字符 ≈0.9k token；省约 19.0k token/请求（95.5%）。
- 两条不变量：① 两种 profile 都完整登记 68 个操作，instr_call 与 legacy 同名工具**共用同一执行路径**（wrapper 逐句等价）；② 护栏不经前端放行，前端只做入参校验（校验失败时执行入口一次都不被调用）。
- 你说"客户端过滤只能作优化、不能作架构基础"是对的。我另外查到：DSH 的 dsh-mcp-client **根本没有工具过滤字段**（只有 serverName/transport/超时/重连），所以服务端 profile 在这个部署里是唯一可行方案。

【阶段 4 设计问题，含我对你上一轮论证的两点异议】

异议 1（关键）：你的安全论证前提在本部署形态下不成立。
你写道"如果 agent 可以 import socket / pyvisa，那在 Python library 里加多少安全检查都没有用"，并据此要求 instr_run 必须跑在无 socket / 无 VISA / 无 import 能力的沙箱里。但**本部署里 agent 已经拥有无限制代码执行能力**：DSH 给 agent 的工具里有 pwsh（任意 PowerShell；本会话 file policy = danger-full-access、审批策略 = never），它现在就能 `python -c "import pyvisa; ..."` 直接操作仪器，或直接对仪器的 5025 端口发 SCPI。也就是说：
- 仪器控制这条链上的"沙箱"在本部署里不构成安全边界——同一个 agent 用 pwsh 一步就绕过了；
- MCP 层的护栏实际上是**纪律与一致性层**（防误操作、留审计、串行化设备访问），不是对抗恶意或失控 agent 的边界。

所以我认为阶段 4 不该以"提供安全隔离"为设计目标——投入很大（Python 沙箱很难做对）却买不到安全。请评估这个判断是否成立；如果不成立，请指出我漏掉了什么（例如你是否假设 pwsh 实际被限制，或你假设的是另一种部署形态）。

异议 2：在这个前提下，instr_run 的收益需要重新论证。
agent 现在就能用 pwsh 写脚本调用设备库——事实上该仓的 TEST_SCRIPTS/ 里已有大量这类脚本。也就是说"模型写代码组合调用"**已经可用**，instr_run 不提供新能力；它提供的是：受控 API（不用模型自己摸索库接口）、统一护栏与审计、以及只把摘要送回上下文。
那么问题变成：instr_run 相对"agent 直接用 pwsh 写脚本"的**增量价值**是什么？值不值得为它建一套运行时？

请给出：
1. 对上述两点异议的评估（同意 / 不同意，理由）。
2. 在这个前提下，阶段 4 应该做成什么形态？请给**一个**明确推荐，并说明它相对"直接用 pwsh"的增量价值。
3. 若仍要保留"受控代码执行"，最小可行实现是什么（约束：Windows + Python + pyvisa + 真实仪器）？
4. 如果你认为阶段 4 应该缩小范围或干脆不做，请直说。我要的是判断，不是把选项罗列一遍。


**A:**
你的两点异议我都接受，其中第一点会推翻我上一轮对阶段 4 的主要安全论证。

## 1. 两点异议的判断

### 异议 1：成立

你给出的部署事实已经足够：

```text
Agent
 ├─ MCP instrument server
 │    └─ policy / audit / lock / verify
 │
 └─ pwsh，danger-full-access，approval=never
      ├─ python + pyvisa
      ├─ socket -> TCP/5025
      └─ 其他本机能力
```

在这个拓扑里，即使 `instr_run` 内部做到：

```text
禁 socket
禁 pyvisa
禁 import
独立进程
严格白名单
```

同一个 agent 仍然可以下一步调用：

```powershell
python -c "import socket; ..."
```

绕过整个 instrument runtime。

因此，`instr_run` 沙箱对**整个 agent 到仪器的攻击面**没有形成 security boundary。

我上一轮隐含的前提确实与你的部署不符。我当时假定大致是：

```text
Agent 能调用 instrument MCP
Agent 没有仪器网络 / VISA / 任意 shell 的旁路能力
```

在那种部署中，受限 runner 有明确的安全意义。你这里没有这个条件。

更准确地说，你现在的 Broker 能覆盖三类风险：

| 风险 | 当前 Broker 是否有效 |
|---|---|
| 模型正常走 MCP 时误发危险命令 | 有效 |
| MCP wrapper / 调用方写错流程 | 有效 |
| 操作审计、验证、错误队列、串行化 | 有效 |
| agent 主动或异常地改走 pwsh/pyvisa/socket | 无法阻止 |
| 本机执行环境被控制后直接碰仪器 | 无法阻止 |

所以我会把现在的定位称为：

> **受信执行路径上的强制策略层和工程一致性层。**

只要进入这条路径，policy 是强制的；它对旁路路径没有控制力。

这里还有一个容易漏掉的问题：

**如果 DSH、Codex、Claude 等客户端各自启动一个 stdio server 进程，那么你现在“同一个 lock 对象”只能序列化单进程内部访问。**

例如：

```text
DSH       -> server.exe A -> Lock A -> DMM
Codex     -> server.exe B -> Lock B -> DMM
pwsh                     -------> DMM
```

三个路径之间不会互斥。

如果这些客户端存在同时操作同一台真实仪器的可能，这个问题的实际风险可能高于 `instr_run` 沙箱问题。届时需要 named mutex、file lock 或单实例 broker daemon，具体取决于你的并发模型。

---

### 异议 2：也成立

在你的环境里，任意代码执行能力已经存在，所以：

```text
instr_run ≠ 新增“模型可以写程序控制仪器”的能力
```

它真正可能增加的是以下几项：

1. **稳定的 canonical API**
   
   模型使用：

   ```text
   sdg.set_wave
   dmm.measure_dc
   ```

   无需知道 `sdg_control`、`keysight_3446x` 内部 Python API。

2. **统一执行路径**

   每一个操作继续经过：

   ```text
   catalog
   policy
   lock
   verify
   audit
   ```

3. **减少 LLM round trip**

   1000 点扫频可以一次提交给服务端执行，避免：

   ```text
   tool call
   model
   tool call
   model
   ...
   ```

4. **结果不进入模型上下文**

   1000 个原始测量点可以留在服务器侧，只返回：

   ```text
   run_id
   count
   min/max
   failures
   artifact path
   summary
   ```

5. **运行语义统一**

   可以统一定义：

   ```text
   timeout
   stop_on_error
   cleanup
   cancellation
   run_id
   step audit
   result persistence
   ```

这些才是阶段 4 值得做的理由。

而其中没有一项要求你实现 Python security sandbox。

---

# 2. 我的明确推荐：缩小阶段 4，做 `instr_batch`，不要做通用 `instr_run(Python)`

我建议直接修改上一轮方案：

> **阶段 4 做“服务端批处理 / plan executor”，暂时取消任意 Python Code Mode。**

接口甚至可以只有第五个 compact tool：

```text
instr_batch(plan)
```

它接受由 canonical operation 组成的声明式执行计划。

例如：

```json
{
  "steps": [
    {
      "op": "sdg.set_frequency",
      "args": {
        "channel": 1,
        "frequency_hz": 1000
      }
    },
    {
      "op": "dmm.measure_voltage",
      "args": {
        "range": "auto"
      },
      "save_as": "v"
    }
  ],
  "on_error": "stop"
}
```

执行路径：

```text
instr_batch
    │
    ├── registry resolve
    ├── schema validate
    │
    └── for each operation
          │
          └── 与 instr_call / legacy 完全相同的执行入口
                 ├── policy
                 ├── locking
                 ├── verify
                 └── audit
```

### 为什么这个东西比直接 pwsh 有实际增量价值

它解决的是**协调成本**。

例如现在模型做 100 点扫频有三种办法。

#### 多次 MCP

```text
100 × set_frequency
100 × measure
```

产生大量 agent round trip。

#### pwsh 脚本

执行效率高，但 agent 必须自己知道：

```python
from sdg_control import ...
from keysight_3446x import ...
```

还必须自行正确处理：

```text
设备发现
锁
超时
错误队列
安全策略
readback
审计
异常 cleanup
```

除非脚本主动重新调用你的 runtime，否则这些语义容易漂移。

#### `instr_batch`

模型只需要知道已经通过 `instr_search/describe` 得到的 canonical operations：

```text
sdg.set_frequency
dmm.measure_voltage
```

然后一次提交。

因此 `instr_batch` 的价值非常明确：

> **让组合调用继承现有 68 项 operation 的全部执行语义，同时消除逐工具 LLM round trip。**

它与你已经完成的前三阶段衔接得非常自然，也不需要另一套 Python 运行时。

---

# 3. `instr_batch` 建议做到什么程度

第一版刻意控制功能。

我建议只支持四个机制：

```text
sequence
foreach
capture
stop_on_error
```

例如：

```json
{
  "foreach": {
    "var": "freq",
    "values": {
      "linspace": [100, 10000, 100]
    }
  },
  "steps": [
    {
      "op": "sdg.set_frequency",
      "args": {
        "channel": 1,
        "frequency_hz": "$freq"
      }
    },
    {
      "op": "dmm.measure_voltage",
      "args": {},
      "capture": ["value"]
    }
  ]
}
```

足够覆盖：

```text
扫频
扫电压
重复测量
多通道采集
参数矩阵
设备配置 + 测量
```

第一版我会明确不做：

```text
arbitrary Python expression
while
eval
exec
任意函数定义
任意 import
复杂 if/else
```

理由主要是工程复杂度和可验证性。

复杂自适应测试，例如：

```python
while error > target:
    调整参数
    重新测量
```

现阶段继续放在 `TEST_SCRIPTS/`，直接 Python 做即可。

没有必要让 `instr_batch` 逐渐演变成一门残缺 Python。

---

## 一个重要实现细节

不要这样：

```python
with DEVICE_LOCK:
    execute_batch()
```

然后 batch 内继续调用现在的 operation wrapper，而 wrapper 又：

```python
with DEVICE_LOCK:
    ...
```

如果当前是普通 `threading.Lock`，直接死锁。

我建议第一版仍保持：

```text
每个 canonical operation 自己获得现有 lock
```

这样满足你最重要的不变量：

> legacy / instr_call / instr_batch 的单次 operation 执行路径完全相同。

以后有证据证明需要整个 sweep 独占仪器，再单独设计：

```text
lease / transaction / lock ownership token
```

不要为了 batch 顺手换成 `RLock` 掩盖锁层次问题。

---

# 4. 建议给 batch 增加的运行语义

这部分比 Python 沙箱更值得投入。

例如：

```json
{
  "deadline_s": 120,
  "max_steps": 1000,
  "on_error": "stop",
  "result_mode": "summary"
}
```

服务器生成：

```text
run_id
started_at
finished_at
operation_count
success_count
failed_step
audit references
result artifact
summary
```

尤其推荐把大结果写：

```text
TEST_RESULTS/<run_id>/...
```

模型上下文只获得统计和路径。

这正好匹配你现有强调留痕的工作方式，同时能显著减少：

```text
仪器 → MCP JSON → 模型 context
```

的数据量。

---

# 5. 关于 raw SCPI

这里我建议继续维持你当前的统一规则。

即便 `instr_batch` 里有：

```json
{
  "op": "instr.raw_query",
  "args": {"command": "..."}
}
```

也必须正常经过：

```text
decide_scpi()
```

不要给 batch 一个“内部调用，所以可信”的特殊通道。

否则它会成为第二个执行路径。

你已经做得很好的一条架构不变量：

> `instr_call` 与 legacy wrapper 共用执行入口。

阶段 4 应继续保持：

```text
legacy
instr_call
instr_batch
      │
      └────── same canonical executor
```

这比给 batch 增加很多功能重要。

---

# 6. 如果仍坚持保留“模型写 Python”，最小实现该怎么做

如果你确实发现声明式 batch 不够用，我建议把目标定义为：

> **execution containment + API convenience**

不要把目标写成 security sandbox。

最小结构：

```text
MCP server / parent
    │
    ├─ owns registry
    ├─ owns policy
    ├─ owns pyvisa/session
    ├─ owns locks
    └─ owns audit
           ▲
           │ IPC
           ▼
    disposable Python worker
```

worker 中的脚本只能通过类似：

```python
instrument.call("sdg.set_frequency", ...)
```

请求父进程执行。

### 为什么仍然用子进程

价值主要有：

```text
脚本死循环可以 kill
异常不会污染 MCP event loop
stdout/stderr 可隔离
可以设置 wall-clock deadline
run 可独立编号
失败可以完整回收
```

这属于**故障隔离**，不是可信安全隔离。

Windows 下最小实现我会选择：

```text
python worker subprocess
stdin/stdout framed JSON RPC
CREATE_NEW_PROCESS_GROUP
必要时 Windows Job Object
```

并限制：

```text
wall timeout
最大 broker calls
最大 stdout/stderr
最大结果字节数
最大执行步骤
```

父进程收到：

```json
{
  "method": "call",
  "op": "dmm.measure_voltage",
  "args": {}
}
```

仍然走唯一 canonical executor。

### 不要做的工作

我明确不建议投入：

```text
AST 安全审计
RestrictedPython
删除 __builtins__
自制 import hook
黑名单 socket
黑名单 ctypes
黑名单 subprocess
```

这些做不到系统级隔离，而你的 agent 本身又已经有 pwsh 旁路，所以收益更低。

如果有一天你真要建立安全边界，需要调整的是**整个 agent 的 capability**：

```text
取消任意 shell / 限制网络 / 限制 VISA
                 +
instrument broker 成为唯一可达路径
```

到那时再讨论 AppContainer、独立账户、网络 ACL、instrument VLAN 等才有意义。

---

# 7. 我现在会怎么改阶段 4 目标

我会把原来的：

```text
阶段4：受控 Code Mode
```

改成：

```text
阶段4：Batch Execution
目标：
1. 多 operation 一次提交
2. 所有调用复用 canonical executor
3. 服务端循环，降低 LLM round trip
4. 大结果服务器侧落盘
5. 统一 run_id / deadline / error / audit
6. 复杂逻辑继续使用 TEST_SCRIPTS
```

这已经足够构成一个完整阶段。

---

# 8. 还有一个我认为现在应该检查的问题

你这轮强调“设备锁是同一个对象”，这个验证对**单个 server.py 进程**成立。

但你有：

```text
DSH
Codex
Claude
opencode
zcode
```

全部使用 stdio。

如果它们可能同时启动 MCP server，建议你马上验证：

```text
是否存在多个 instrument server 进程同时连接同一台仪器？
```

如果答案为是，则：

```python
_DEVICE_LOCK = threading.Lock()
```

无论迁移得多漂亮，都无法保证跨进程串行化。

这时真正值得增加的 stage 4.5 可能只是几十行：

```text
instrument identity
      ↓
named interprocess mutex
```

或者以后改为唯一的本地 broker daemon。

这个问题和 arbitrary pwsh 一样说明了一件事：**当前 policy 的保护域应该明确写进设计文档。**

例如：

```text
Policy scope:
- 强制覆盖 legacy MCP / compact call / batch
- 保证单 server process 内串行化
- 不防止具有主机代码执行权限的 agent 直接访问 VISA/TCP
- 不保证多个独立 server process 间互斥
```

最后一条如果之后实现跨进程锁，再删除。

---

## 最终判断

我的建议比上一轮明显收缩：

**阶段 4 值得做，但只做 `instr_batch` / plan executor。暂时不要做 Python `instr_run`。**

你现在已经有成熟的 `TEST_SCRIPTS/ + pwsh + Python` 路径，重新造一个 Python execution runtime 的边际收益不足。`instr_batch` 恰好填补现有真正缺口：**在 compact MCP 内进行高效组合执行，同时完整继承已有的 registry、policy、verify、audit 与稳定 canonical API。**

等实际日志证明 batch 无法覆盖大量任务，再增加 worker 型 Code Mode。不要提前建。

## Q3 2026-09-23 16:13:36
**Q:** 你接受了异议、并把阶段 4 收缩为 `instr_batch`，这个方向我同意。但你的回答里有**一处事实过时**和**三处技术判断我不同意**，需要再过一轮。

## 一、事实过时：跨进程锁不是"待验证"，2026-09-17 就已实现并落地

你写"建议你马上验证是否存在多个 instrument server 进程同时连接同一台仪器"，并把它列为可能的 stage 4.5（named mutex）。

实际上本仓已有 `common/session_lock.py`（2026-09-17），并且正是你描述的那个问题：
- 实测结论（`docs/visa_concurrency_20260917.md`）：**同一台设备开两个会话并发会静默串台**——两个客户端互相读到对方的响应，而响应本身都是合法值，调用方判断不出来（DG832/USB 实测约一半查询丢响应 + 100+ 次答非所问）。当日本机实测**同时存在 14 个 instrument MCP 实例**。
- 已实现：按 **VISA 地址**为键的跨进程锁文件（`session_locks/<addr>.json`，含 pid/started/ts），"活跃"判据 = pid 仍存在 且 ts 在 TTL(180s) 内，过期/死进程的锁文件顺手清理。
- **关键：这份实现是"只告警、不阻塞"，且是用户明确指定的口径**——理由是"仪器共享是常见需求，硬拦会误伤；调用方看到 warning 自行决定"。每次设备调用都会把自己的占用刷新，并把他人占用作为 `warnings` 附在返回体里。

所以：
1. 请评估——在你不知道这条已存在的情况下建议的"named mutex / 单实例 broker daemon"，是否与"只告警不阻塞"这个已定口径冲突？如果冲突，你要论证的是"应当改变这个既有决策"（例如指出告警不足的具体后果），而不是把它当作新建议。请给出明确判断。
2. 你最后那条"把 policy scope 明确写进设计文档"我**采纳**，并且会包含你列的边界（强制覆盖 legacy/call/batch；只保证单进程内串行化；不防有主机代码执行权的 agent 直连 VISA/TCP；**跨进程只告警不阻塞**）。这一条我认为是你本轮最有价值的产出。

## 二、技术判断分歧 1：batch 的原子性——我不同意"每个 operation 各自获取锁"

你警告不要 `with DEVICE_LOCK: execute_batch()`（会与 wrapper 内层重复获取而死锁），这个**是对的**，本仓 `_DEVICE_LOCK` 确实是普通 `threading.Lock`（不可重入）。然后你建议"第一版仍保持：每个 canonical operation 自己获得现有 lock"。

但本仓的实际串行化机制不是这把锁，而是**单 worker 执行器 + BUSY 闸门**：
- `_DeviceExecutor.run()` 在入口检查 `if self._busy → 立即返回 device_busy`，否则置 `_busy=True` 并把这个 job 交给唯一 daemon worker 线程；job 完成后由事件循环回填并清 BUSY。
- server.py 里那句注释写得很明确：设备锁"单 worker 下正常情况下没有竞争者，此处 acquire(timeout) 只作**第二道防线**"。
- 每个 operation 的实现内部调 `_call(...)`，由 `_call` 去 acquire `_DEVICE_LOCK`。

由此我有一个**与你相反的方案**，请你判断哪个对：

> **把整个 batch 作为"一个 executor job"提交**，在其内部直接调用各 operation 的实现（`op.fn(**args)`）；每个 op 内部的 `_call` 仍照常 acquire/release `_DEVICE_LOCK`（**没有嵌套**，因为 batch 自己不持锁）。

我的理由：
- **原子性来自 BUSY 而不是锁**：整批作为一个 job ⇒ BUSY 在整批期间保持 ⇒ 进程内其它调用得到 device_busy 而不会插进扫频中间，仪器状态不会被撕开；这正是一次 sweep 需要的语义。
- **不引入死锁**：batch 自己不持有 `_DEVICE_LOCK`，所以内层 `_call` 获取它没有问题。
- **保持你我都看重的不变量**：每个 operation 仍走它自己的 canonical 执行入口，`legacy / instr_call / instr_batch` 的单次 operation 路径完全相同。

而按你的建议（每步各提交一个 executor job），**BUSY 会在步与步之间释放**——别的调用可以插进扫频中途改频率，扫频结果就被污染了。对"配置→测量→配置→测量"这类序列这是实打实的正确性问题。

请明确回答：一个 job 还是每步一个 job？若你仍坚持每步一个，请说明在"总线/仪器共享、多客户端可能同时调用"的前提下，如何保证扫频不被插入。

## 三、技术判断分歧 2：`deadline_s` 与本仓既有的超时语义冲突

你建议 batch 支持 `deadline_s`。但本仓执行器的超时语义是**刻意**这样的（写在模块级说明与返回体里）：

> 超时**只代表放弃等待**，不清 BUSY，**worker 仍在跑**——设备保持 BUSY 直到那次操作真正结束；返回体明确写"操作**可能仍在执行或随后成功**，有副作用的命令请回读确认"。

这是为了顶住"驱动挂起、worker 卡在 native 调用里"的场景（也是它不用 ThreadPoolExecutor 而用 daemon worker 的原因）。

所以 batch 的 `deadline_s` 若照字面实现，会出现：**超时返回"已放弃"，但仪器还在继续执行撤不掉的 1000 点扫频**。这对调用方是误导——正是本仓历史上被反复教训的那类"看起来合理的假结论"。

请给出判断：batch 的 deadline 应当
（a）沿用现有语义（放弃等待、不终止执行、BUSY 保持）并在返回体里说清楚；
（b）在**每个 step 之间**设可取消点（step 边界处检查 deadline，超时则不再发下一条，已发出的那条仍可能完成）；
（c）真取消（需要中断 native 调用——我认为本仓做不到，也不该假装能做）。
我倾向 (b)：它给出"最坏多做一步"的明确上界，且不假装能中断底层。请评估。

## 四、技术判断分歧 3：plan 里的变量替换与校验顺序

你给的例子把 `"frequency_hz": "$freq"` 作为字符串放进参数（类型本应是 number）。这在我已实现的 `instr_call` 校验器下**会先被类型校验拒掉**。所以 batch 必须先替换、再校验，顺序不能反。另外你的 `"capture": ["value"]` 依赖知道每个工具的返回体形状（本仓工具返回的是 JSON 字符串，成功时形如 `{"ok":true,"model":...,"result":...}`，而 `result` 的形状各工具不同：有的是 dict、有的带 `items`、有的只是字符串）。

请给出：
1. 变量替换与 schema 校验的先后（确认先替换后校验即可，还是有别的坑）。
2. **capture 应当怎么设计才不依赖"模型猜返回体形状"**？我的初步想法是 route 走 `instr_describe` 里给出每个 operation 的结果字段说明，或者干脆让 capture 只支持"整条结果"和"指定 JSON path"，请你判断哪种更稳。
3. batch plan 的 schema 用 JSON Schema 表达（嵌套 foreach/steps）是否够——还是应当定义一个小的 plan DSL 版本号以便将来演进？

请仍然给明确结论，不要罗列所有可能。另外：如果你认为我的方案（二、三 两处）比你的更好，直接说；我不要客套。


**A:**
这轮结论很明确：**你在 batch 原子性和 deadline 两处的方案都优于我上一轮的建议。** 阶段 4 可以据此收敛。

## 一、跨进程锁：维持“只告警不阻塞”，不改既有决策

我上一轮提出 named mutex / 单实例 broker daemon，确实和你已经拍板并实现的“跨进程只告警、不阻塞”冲突。基于你补充的背景，我**不建议阶段 4 改变这个决策**。

原因是这里已经属于明确的产品语义：

```text
单进程内：强制串行
跨进程间：允许共享，检测竞争并告警
```

硬互斥会直接改变现有使用模型。

但已有实测也说明一个事实：跨进程竞争时可能出现**合法但属于另一请求的响应**。因此 warning 代表的不只是“有人也在用”，它意味着该次测量的完整性可能无法证明。

所以 batch 要做一件额外的事：

> **聚合并显式暴露整个 run 期间观察到的跨进程 contention，绝不能让 warning 淹没在某个中间 step 的返回值里。**

例如最终结果至少带：

```json
{
  "ok": true,
  "warnings": [
    {
      "code": "external_session_contention",
      "address": "...",
      "steps": [12, 13, 14]
    }
  ]
}
```

如果愿意再明确一点，可以增加：

```json
"integrity": "contended"
```

但不要因此把 `ok` 自动改成 false，因为这又改变了既定的“告警，由调用方裁决”语义。

你准备写入 policy scope 的四条边界是准确的。

---

# 二、batch 调度：**整个 batch 一个 executor job**

这里你的方案对。

正确结构应当是：

```text
instr_batch
    │
    ├─ plan/front-end validation
    │
    └─ _DeviceExecutor.run(batch_job)    ← 只提交一次
                  │
                  │ BUSY=true 整批保持
                  ▼
             batch worker
                  │
                  ├─ op1.fn(...)
                  │    └─ _call()
                  │         └─ acquire/release _DEVICE_LOCK
                  │
                  ├─ op2.fn(...)
                  │    └─ _call()
                  │
                  ...
                  ▼
             batch complete
                  │
                  └─ BUSY=false
```

这同时满足三个目标。

第一，**整批期间进程内不可插入其他设备 job**。

对于：

```text
set_frequency
measure
set_frequency
measure
```

这是正确性要求。若 step 之间释放 BUSY，其他调用插入：

```text
batch: set 1 kHz
other: set 10 kHz
batch: measure
```

测量语义直接失效。

第二，batch 本身不持 `_DEVICE_LOCK`，所以每个 operation 内部 `_call()` 正常获取普通 `threading.Lock`，没有重入死锁。

第三，operation 的实际设备调用路径继续完全复用：

```text
legacy
instr_call
instr_batch
    ↓
同一个 operation implementation
    ↓
_call
    ↓
policy / lock / verify / audit
```

只有**调度粒度**发生变化。

有一点术语我建议写准确：

> batch 提供的是 **进程内 non-interleaving / exclusive sequence**，不要称为事务原子性。

因为：

```text
step 1 成功
step 2 成功
step 3 失败
```

前两步不会回滚。

而且跨进程仍然可能插入。

所以它的保证准确表述应当是：

```text
同一个 instrument MCP server 进程内，
batch 从第一个 step 到最后一个 step 期间不会被其他 device executor job 插入。
```

---

## 一个实现上的关键点

batch worker 内不要重新调用公开的：

```python
instr_call(...)
```

如果 `instr_call` 自己还会：

```python
_DeviceExecutor.run(...)
```

就会形成 executor 自调用。

应该调用 executor 内部原本用于执行单个 operation 的那层函数。

如果目前还没有明确分层，我会整理成类似：

```python
def _invoke_operation(op, args):
    # 已进入 worker
    return op.fn(**args)
```

于是：

```text
instr_call
    -> executor.run(lambda: _invoke_operation(...))

instr_batch
    -> executor.run(lambda: _run_batch(...))
                              |
                              +-> _invoke_operation(...)
                              +-> _invoke_operation(...)
```

这样结构最干净。

---

# 三、deadline：选择 **(b) step 边界协作式停止**

这里你的判断也更好。

我明确排除：

### (c) 真取消

如果 worker 已经进入：

```text
pyvisa
vendor DLL
USB backend
socket recv
native driver
```

Python 层没有可靠手段把这个调用安全中断。

声称“cancelled”会制造错误事实。

### (a) 单纯沿用“调用方放弃等待”

对于单 operation 可以接受。

对于 1000 step batch，语义过差：

```text
调用方 30 秒 timeout
返回“放弃等待”
worker 又运行 20 分钟
继续不断修改仪器状态
```

调用方几乎无法判断设备未来还会执行多少动作。

因此 batch 应增加一个**执行侧 cooperative deadline**。

---

## 我推荐的精确定义

假设：

```json
"deadline_s": 120
```

worker 在**每个 leaf operation 开始之前**检查：

```python
if monotonic() >= deadline:
    stop_batch(reason="deadline_reached")
```

已经开始的 operation：

```text
不尝试中断
等待它自然返回
随后不再开始下一步
```

因此上界是：

> deadline 到达以后，最多继续完成一个已经启动的 batch step。

这里还要再精确一层：

**一个 step 可能内部发送多条 SCPI。**

所以真正保证的是：

```text
最多继续完成一个 canonical operation
```

不能承诺：

```text
最多再发一条 SCPI
```

这点值得直接写进文档。

---

## 不要把两种 timeout 混成一个概念

你目前已有：

```text
wait timeout
```

语义：

```text
调用方停止等待
worker 继续运行
BUSY 保持
```

batch 新增的是：

```text
execution deadline
```

语义：

```text
worker 在 operation 边界停止启动新 step
当前 operation 不可强杀
```

我甚至建议字段名称不要继续叫裸的 `timeout_s`。

例如明确区分：

```json
{
  "execution_deadline_s": 120
}
```

已有 executor 的：

```text
wait_timeout
```

继续保持原语义。

这样以后日志不会出现这种模糊描述：

```text
batch timeout
```

让人不知道到底是 caller 不等了，还是 execution 已进入停止状态。

---

# 四、变量替换：**先结构校验，再变量解析，再 operation schema 校验**

你指出的 `$freq` 问题成立。

如果：

```json
"frequency_hz": "$freq"
```

直接送入 FastMCP schema：

```json
"type": "number"
```

必然应该失败。

所以 operation 级别的校验顺序必须是：

```text
plan structural validation
        ↓
变量解析 / foreach binding
        ↓
得到实际 JSON value
        ↓
operation schema validation
        ↓
execute
```

但我会改掉上一轮那个字符串 `$freq` 设计。

## 不建议字符串模板变量

不要：

```json
"frequency_hz": "$freq"
```

建议用显式 typed reference：

```json
"frequency_hz": {
  "$var": "freq"
}
```

如果：

```text
freq = 1000.0
```

替换以后节点就是 JSON number：

```json
"frequency_hz": 1000.0
```

类型天然正确。

这还能避免：

```text
"$freq"
"abc-$freq"
"$$freq"
```

这一类模板字符串转义和类型转换问题。

v1 最好完全不支持字符串插值。

如果需要字符串参数，就让变量本身是字符串：

```json
{
  "path": {"$var": "output_path"}
}
```

---

# 五、capture：不要依赖模型猜结果结构

我不建议把正确性建立在：

> 模型先看 `instr_describe`，记住返回字段，再写 capture

这个方式上。

`instr_describe` 可以帮助模型，但它属于知识提示层，不适合成为执行语义的依赖。

我推荐的 v1 capture 只有两种：

```text
1. 整个 result
2. result 内明确 JSON Pointer
```

例如：

```json
"capture": {
  "var": "measurement",
  "source": "result"
}
```

捕获完整：

```json
response["result"]
```

或者：

```json
"capture": {
  "var": "voltage",
  "source": "result",
  "pointer": "/value"
}
```

使用 RFC 6901 JSON Pointer。

我会选 **JSON Pointer，不选 JSONPath**。

原因很简单：

```text
JSON Pointer：确定、简单、标准化、实现小
JSONPath：查询语言更强，同时也复杂很多
```

batch v1 不需要过滤器、通配符和表达式。

---

## 返回体先由 server 解析

既然 operation 当前统一返回 JSON 字符串：

```json
{
  "ok": true,
  "model": "...",
  "result": ...
}
```

batch 应在 server 内：

```python
outcome = json.loads(operation_return)
```

然后 capture 操作结构化对象。

不要让 plan 针对序列化字符串操作。

如果：

```text
pointer 不存在
result 不是预期 JSON 类型
返回体不是合法 JSON
```

直接产生明确的 step failure，例如：

```text
capture_pointer_not_found
invalid_operation_response
```

不要尝试猜字段。

---

## `instr_describe` 的角色

仍然值得在 describe 中逐步加入：

```text
result description
常用 capture pointer
example
```

例如：

```json
{
  "result": {
    "description": "...",
    "common_pointers": [
      "/value",
      "/unit"
    ]
  }
}
```

但这是**可发现性增强**。

batch engine 本身只认识：

```text
完整 result
JSON Pointer
```

两者要分开。

---

# 六、plan 应定义为 **版本化 JSON DSL**

这一点我建议直接定下来：

> **JSON 是语法载体，JSON Schema 是结构验证器，整个 plan 应从 v1 起就定义为一个有版本号的小 DSL。**

例如：

```json
{
  "plan_version": 1,
  "steps": [...]
}
```

不需要创造文本语言。

继续用 JSON 即可。

原因是 JSON Schema 只能很好地表达：

```text
字段类型
required
oneOf
嵌套结构
递归 steps
```

但下面这些属于**语言语义**：

```text
变量作用域
foreach 绑定
capture 生命周期
变量遮蔽
step 执行顺序
deadline 检查点
错误传播
跨进程 warning 聚合
最大展开步数
```

这些无法仅靠 JSON Schema 定义完整。

因此应有：

```text
Batch Plan DSL v1
```

以及配套：

```text
plan.schema.json
execution_semantics.md
```

---

# 七、我建议 v1 把语义再收紧一点

我会给 v1 定下面这些硬规则。

### 变量作用域

```text
foreach var：仅其 steps 内可见
capture var：capture 后的后续 sibling steps 可见
```

禁止隐式全局污染。

### 禁止同层变量覆盖

例如已经存在：

```text
freq
```

再次 capture：

```text
freq
```

直接 plan validation error。

不要悄悄覆盖。

### 展开上限

即使不用安全沙箱，也应防止意外：

```text
1000 × 1000 nested foreach
```

建议 execution 前或运行中强制：

```text
max_leaf_steps
max_nesting_depth
```

这属于工程资源保护。

### 每个实际 operation 调用前重新 schema validate

即：

```text
foreach binding
capture substitution
        ↓
最终 args
        ↓
FastMCP-derived schema
```

你的阶段 1 已经保证 schema 单一来源，这里正好复用，不需要另写规则。

---

# 八、最终建议的阶段 4 结构

我现在会把阶段 4 定义成下面这样：

```text
instr_batch(plan)
        │
        ├─ validate Batch Plan DSL v1
        │
        └─ submit ONE executor job
                    │
                    │ BUSY held for whole batch
                    ▼
              batch interpreter
                    │
       ┌────────────┼────────────┐
       │            │            │
   foreach       capture      deadline
       │            │       boundary check
       └────────────┼────────────┘
                    ▼
              resolve variables
                    ▼
            FastMCP schema validate
                    ▼
          canonical operation invoke
                    ▼
       policy / _DEVICE_LOCK / verify / audit
```

最终保证写成四条就很清楚：

1. **单进程内 batch 整体不可被其他 executor job 插入。**
2. **跨进程继续只告警，不提供互斥保证。**
3. **execution deadline 仅在 operation 边界停止后续步骤，不强杀已开始的 operation。**
4. **所有 operation 在变量解析后继续使用现有 FastMCP 派生 schema 与同一执行路径。**

所以这一轮对争议点的裁决是：

- 跨进程：**维持现有 warning-only 决策，我上一轮的 mutex 建议撤回。**
- batch 调度：**你的一整个 batch 一个 executor job 更正确。**
- deadline：**你提出的 (b) 更正确。**
- 变量：**先解析成真实 typed value，再做 operation schema 校验。**
- capture：**整 result + 显式 JSON Pointer，describe 只帮助发现，不承担正确性。**
- plan：**从第一版就叫/version 成 JSON Batch Plan DSL v1，同时用 JSON Schema 校验结构。**

这套已经足够开始阶段 4，实现边界也比较干净。

## Q4 2026-09-23 16:35:06
**Q:** 设计已经收敛，这轮请给可实现的**具体产物**，不要再说原则。

## 一、请给出 Batch Plan DSL v1 的完整 JSON Schema

要求：
- 直接给出可用的 `plan.schema.json` 文本（JSON Schema draft 2020-12 即可）；
- 覆盖你已经裁定的机制：`plan_version`、`steps`（含嵌套）、`foreach`、`capture`、`on_error`、`execution_deadline_s`、`max_leaf_steps`、`max_nesting_depth`；
- 变量引用用你裁定的 typed reference `{"$var": "..."}`——请把它的 schema 写清楚，并说明它**允许出现在哪些位置**（只能作整个值？还是允许出现在数组元素里？）；
- capture 用整 result 或 `result` + RFC 6901 JSON Pointer 两种形态，也用 schema 表达。

## 二、给 3 个真实示例 plan（针对本仓已登记的 operation）

本仓的 operation 命名是 `<设备族>.<操作>`，例如 `sdg.set_wave`、`sds_measure`、`dmm_measure`、`psu_output`、`dg_protect`（参数表可用 instr_describe 取）。请写：

1. **单设备扫频**：SDG 逐频点设波形 → DMM 测电压 → 收集 (频率, 电压)，约 20 点。
2. **示波器批采**：SDS 改时基/档位若干次 → 每次 `sds_measure` 取一个量 → 只保留统计。
3. **一个反例**：一个**应当被 v1 拒绝**的 plan（例如含 while、或变量作用域越界、或 capture 到未定义变量），并说明拒绝发生在哪一步（结构校验 / 绑定期 / operation schema 校验）。

## 三、请你裁定三个我自己拿不准的边界

1. **`max_leaf_steps` 何时算**：预展开（先把 foreach 全部展开再校验总数）还是边执行边计数？预展开的好处是"超限就不开始执行"（不会做到一半才失败），坏处是嵌套很深时展开本身可能巨大。你会选哪个？
2. **`capture` 的可见范围**：你说"capture 后的后续 sibling steps 可见"。那**嵌套 foreach 的内层**能否 capture 给外层后续步骤用？如果不能，模型很容易写出"内层捕获、外层使用"这种看不出错的 plan 而静默拿不到值。请给一个不会静默出错的规则（要么允许并明确定义，要么在**校验期**就拒绝）。
3. **错误传播与 `on_error`**：`on_error: stop` 时，已 capture 的变量与已产生的 partial results 是丢弃、还是随 run 一起返回？我倾向"随 run 返回并标记 run 未完成"（仪器侧的副作用已经发生，隐瞒 partial 结果会让调用方误判设备状态），请你判断。

请直接给 schema 文本与示例，边界问题给结论。

**A:**
你的二、三处裁决我按上一轮结论固定下来。下面这版可以直接作为 v1 实现基线。

先说明一个局限：我这里拿不到你本机 `instr_describe` 的具体函数签名，所以示例中的 canonical op 按你给出的命名规则写，`args` 键采用典型命名。DSL、作用域和验证顺序可以直接落地，具体参数键需要用本仓 `instr_describe` 对齐一次。`sds_measure`、`dmm_measure` 等 legacy 名在 plan 中应使用 canonical `sds.measure`、`dmm.measure`。

## 一、`plan.schema.json`

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "urn:instrument-runtime:batch-plan:v1",
  "title": "Instrument Batch Plan DSL v1",
  "type": "object",
  "additionalProperties": false,
  "required": [
    "plan_version",
    "steps",
    "on_error",
    "max_leaf_steps",
    "max_nesting_depth"
  ],
  "properties": {
    "plan_version": {
      "const": 1
    },
    "steps": {
      "$ref": "#/$defs/steps"
    },
    "on_error": {
      "$ref": "#/$defs/onError"
    },
    "execution_deadline_s": {
      "type": "number",
      "exclusiveMinimum": 0
    },
    "max_leaf_steps": {
      "type": "integer",
      "minimum": 1
    },
    "max_nesting_depth": {
      "type": "integer",
      "minimum": 0
    }
  },
  "$defs": {
    "identifier": {
      "type": "string",
      "pattern": "^[A-Za-z_][A-Za-z0-9_]*$",
      "minLength": 1,
      "maxLength": 64
    },

    "operationId": {
      "type": "string",
      "pattern": "^[A-Za-z][A-Za-z0-9_]*\\.[A-Za-z][A-Za-z0-9_]*$"
    },

    "onError": {
      "type": "string",
      "enum": [
        "stop",
        "continue"
      ]
    },

    "varRef": {
      "type": "object",
      "additionalProperties": false,
      "required": [
        "$var"
      ],
      "properties": {
        "$var": {
          "$ref": "#/$defs/identifier"
        }
      }
    },

    "literalScalar": {
      "type": [
        "string",
        "number",
        "integer",
        "boolean",
        "null"
      ]
    },

    "literalArray": {
      "type": "array",
      "items": {
        "$ref": "#/$defs/literalValue"
      }
    },

    "literalObject": {
      "type": "object",
      "propertyNames": {
        "not": {
          "const": "$var"
        }
      },
      "additionalProperties": {
        "$ref": "#/$defs/literalValue"
      }
    },

    "literalValue": {
      "oneOf": [
        {
          "$ref": "#/$defs/literalScalar"
        },
        {
          "$ref": "#/$defs/literalArray"
        },
        {
          "$ref": "#/$defs/literalObject"
        }
      ]
    },

    "runtimeArray": {
      "type": "array",
      "items": {
        "$ref": "#/$defs/runtimeValue"
      }
    },

    "runtimeObject": {
      "type": "object",
      "propertyNames": {
        "not": {
          "const": "$var"
        }
      },
      "additionalProperties": {
        "$ref": "#/$defs/runtimeValue"
      }
    },

    "runtimeValue": {
      "oneOf": [
        {
          "$ref": "#/$defs/varRef"
        },
        {
          "$ref": "#/$defs/literalScalar"
        },
        {
          "$ref": "#/$defs/runtimeArray"
        },
        {
          "$ref": "#/$defs/runtimeObject"
        }
      ]
    },

    "args": {
      "type": "object",
      "propertyNames": {
        "not": {
          "const": "$var"
        }
      },
      "additionalProperties": {
        "$ref": "#/$defs/runtimeValue"
      }
    },

    "captureWholeResult": {
      "type": "object",
      "additionalProperties": false,
      "required": [
        "var",
        "source"
      ],
      "properties": {
        "var": {
          "$ref": "#/$defs/identifier"
        },
        "source": {
          "const": "result"
        }
      }
    },

    "captureResultPointer": {
      "type": "object",
      "additionalProperties": false,
      "required": [
        "var",
        "source",
        "pointer"
      ],
      "properties": {
        "var": {
          "$ref": "#/$defs/identifier"
        },
        "source": {
          "const": "result"
        },
        "pointer": {
          "type": "string",
          "pattern": "^(?:/(?:[^~/]|~[01])*)+$"
        }
      }
    },

    "capture": {
      "oneOf": [
        {
          "$ref": "#/$defs/captureWholeResult"
        },
        {
          "$ref": "#/$defs/captureResultPointer"
        }
      ]
    },

    "operationStep": {
      "type": "object",
      "additionalProperties": false,
      "required": [
        "op"
      ],
      "properties": {
        "op": {
          "$ref": "#/$defs/operationId"
        },
        "args": {
          "$ref": "#/$defs/args"
        },
        "capture": {
          "$ref": "#/$defs/capture"
        }
      }
    },

    "foreachSpec": {
      "type": "object",
      "additionalProperties": false,
      "required": [
        "var",
        "values"
      ],
      "properties": {
        "var": {
          "$ref": "#/$defs/identifier"
        },
        "values": {
          "type": "array",
          "minItems": 1,
          "items": {
            "$ref": "#/$defs/literalValue"
          }
        }
      }
    },

    "foreachStep": {
      "type": "object",
      "additionalProperties": false,
      "required": [
        "foreach",
        "steps"
      ],
      "properties": {
        "foreach": {
          "$ref": "#/$defs/foreachSpec"
        },
        "steps": {
          "$ref": "#/$defs/steps"
        }
      }
    },

    "step": {
      "oneOf": [
        {
          "$ref": "#/$defs/operationStep"
        },
        {
          "$ref": "#/$defs/foreachStep"
        }
      ]
    },

    "steps": {
      "type": "array",
      "minItems": 1,
      "items": {
        "$ref": "#/$defs/step"
      }
    }
  }
}
```

### `$var` 的精确定义

v1 中：

```json
{"$var": "freq_hz"}
```

必须占据一个**完整 JSON value 节点**。

这些合法：

```json
{
  "frequency": {"$var": "freq_hz"},
  "channels": [1, {"$var": "channel"}],
  "config": {
    "scale": {"$var": "scale_v"}
  }
}
```

这些非法或不具有变量语义：

```json
{
  "frequency": "${freq_hz}",
  "name": "freq=$freq_hz",
  "$var": "freq_hz",
  "other": 1
}
```

允许 `$var` 出现的位置限定为：

```text
operationStep.args 的任意 value 节点
包括嵌套 object value
包括 array element
```

v1 **不允许** `$var` 出现在：

```text
foreach.values
foreach.var
op
capture.var
capture.pointer
execution_deadline_s
max_leaf_steps
max_nesting_depth
```

特别是 `foreach.values` 必须全部静态可知，这使 leaf 数量可以在执行前精确计算。

---

# 二、三个示例 plan

## 1. SDG 扫频，加 DMM 测量

20 个频点，每个频点两次 leaf operation，所以精确 leaf 数为 40。

```json
{
  "plan_version": 1,
  "on_error": "stop",
  "execution_deadline_s": 120,
  "max_leaf_steps": 40,
  "max_nesting_depth": 1,
  "steps": [
    {
      "foreach": {
        "var": "freq_hz",
        "values": [
          100,
          150,
          220,
          330,
          470,
          680,
          1000,
          1500,
          2200,
          3300,
          4700,
          6800,
          10000,
          15000,
          22000,
          33000,
          47000,
          68000,
          100000,
          150000
        ]
      },
      "steps": [
        {
          "op": "sdg.set_wave",
          "args": {
            "channel": 1,
            "waveform": "SINE",
            "frequency_hz": {
              "$var": "freq_hz"
            },
            "amplitude_vpp": 1.0,
            "offset_v": 0.0
          }
        },
        {
          "op": "dmm.measure",
          "args": {
            "mode": "ac_voltage"
          },
          "capture": {
            "var": "voltage_result",
            "source": "result"
          }
        }
      ]
    }
  ]
}
```

这里建议运行器自动给每个 leaf result 附当前 foreach bindings：

```json
{
  "op": "dmm.measure",
  "bindings": {
    "freq_hz": 1000
  },
  "result": {
    "...": "..."
  }
}
```

这样频率和测量结果天然形成对应关系，无需给 DSL 增加 `append()`。

`voltage_result` 的 capture 在本次 iteration 内可供后续 step 使用；foreach 结束后失效。

如果你的 `dmm.measure` 实际 result 明确为：

```json
{
  "value": 1.0023,
  "unit": "V"
}
```

可以改成：

```json
"capture": {
  "var": "voltage_v",
  "source": "result",
  "pointer": "/value"
}
```

---

## 2. SDS 多组时基和垂直档位，只执行 measurement

这里用两层 foreach：

```text
4 timebases × 3 scales ×
    1 set_scale
    1 measure
+
4 set_timebase
= 28 leaf operations
```

```json
{
  "plan_version": 1,
  "on_error": "stop",
  "execution_deadline_s": 90,
  "max_leaf_steps": 28,
  "max_nesting_depth": 2,
  "steps": [
    {
      "foreach": {
        "var": "timebase_s",
        "values": [
          0.000001,
          0.00001,
          0.0001,
          0.001
        ]
      },
      "steps": [
        {
          "op": "sds.set_timebase",
          "args": {
            "timebase_s": {
              "$var": "timebase_s"
            }
          }
        },
        {
          "foreach": {
            "var": "scale_v",
            "values": [
              0.1,
              0.5,
              1.0
            ]
          },
          "steps": [
            {
              "op": "sds.set_scale",
              "args": {
                "channel": 1,
                "scale_v": {
                  "$var": "scale_v"
                }
              }
            },
            {
              "op": "sds.measure",
              "args": {
                "channel": 1,
                "item": "VPP"
              }
            }
          ]
        }
      ]
    }
  ]
}
```

这里“只保留统计”的 v1 解释建议定成：

```text
不调用 waveform/raw acquisition
只保存 sds.measure 的结构化 leaf result
```

batch DSL 本身暂时不增加 `mean/min/max/reduce` 运算。服务端最终 summary 可以统计：

```text
total
success
failed
elapsed
```

具体测量值仍存在 `leaf_results` 中。

如果你这里的“只保留统计”是指 **12 次 VPP 只返回 min/max/mean，连每次 VPP 都不返回模型**，那属于 result presentation 层，建议后续单独加 `result_mode`，不要塞进执行 DSL v1。

---

## 3. 应拒绝的作用域越界 plan

```json
{
  "plan_version": 1,
  "on_error": "stop",
  "max_leaf_steps": 10,
  "max_nesting_depth": 1,
  "steps": [
    {
      "foreach": {
        "var": "freq_hz",
        "values": [
          1000,
          2000
        ]
      },
      "steps": [
        {
          "op": "dmm.measure",
          "args": {},
          "capture": {
            "var": "measured",
            "source": "result"
          }
        }
      ]
    },
    {
      "op": "sdg.set_wave",
      "args": {
        "frequency_hz": {
          "$var": "measured"
        }
      }
    }
  ]
}
```

这个 plan：

```text
JSON Schema 结构校验：通过
operation registry lookup：通过
静态变量绑定/作用域校验：失败
```

失败原因：

```text
undefined_variable: measured
```

`measured` 只存在于 foreach 每个 iteration 的局部作用域。foreach 退出后没有唯一的 `measured` 值，因为实际上产生了两个：

```text
iteration 0 -> measured₀
iteration 1 -> measured₁
```

必须在**提交 executor job 之前**拒绝，所以仪器调用次数应为 0。

我建议错误位置直接输出 JSON Pointer 风格路径：

```text
/steps/1/args/frequency_hz/$var
```

返回类似：

```json
{
  "ok": false,
  "error": "undefined_variable",
  "variable": "measured",
  "path": "/steps/1/args/frequency_hz/$var"
}
```

---

# 三、三个边界的最终裁决

## 1. `max_leaf_steps`：执行前精确计数，但不展开

我选：

> **预计算 cardinality，不 materialize 展开的 plan。**

这是第三种实现方式，比“完整预展开”和“边执行边计数”都合适。

计数函数：

```python
def count_steps(steps, limit):
    total = 0

    for step in steps:
        if "op" in step:
            total += 1

        else:
            n = len(step["foreach"]["values"])
            inner = count_steps(step["steps"], limit)

            if inner > limit // n:
                raise TooManyLeafSteps()

            total += n * inner

        if total > limit:
            raise TooManyLeafSteps()

    return total
```

例如：

```text
foreach 20
    set_wave       1
    measure        1

count = 20 × (1 + 1) = 40
```

嵌套：

```text
foreach 4
    set_timebase                       1
    foreach 3
        set_scale                      1
        measure                        1

count = 4 × (1 + 3 × (1 + 1))
      = 28
```

内存复杂度跟 AST 大小相关，不跟展开后的 leaf 数相关。

因此：

```text
max_leaf_steps 超限
```

一定发生在：

```text
提交 executor job 之前
仪器调用次数 = 0
```

而且建议服务端还有独立 hard limit，例如：

```python
effective_limit = min(
    plan["max_leaf_steps"],
    SERVER_MAX_LEAF_STEPS
)
```

plan 不能通过写一个巨大 `max_leaf_steps` 绕过服务器资源限制。

---

## 2. capture 作用域：禁止从 foreach 内逃逸到外层

我选择严格词法作用域。

规则定成：

```text
外层变量
    ↓
foreach 内可见

foreach var
    ↓
当前 foreach body 内可见
    ↓
其嵌套 foreach 内也可见

capture
    ↓
同一 lexical block 中，其后的 sibling steps 可见
    ↓
这些 sibling 的子块也可见

foreach 内产生的 capture
    ↓
foreach 外不可见
```

例如合法：

```text
foreach freq
    measure -> capture v
    configure_using(v)        OK

    foreach gain
        use(freq)              OK
        use(v)                 OK
```

非法：

```text
foreach freq
    measure -> capture v

use(v)                         ERROR
```

原因是多个 iteration 会产生多个 `v`，没有合理的单值提升规则。

### 必须静态拒绝

这里不能运行到那里才发现。

建议 preflight semantic validator 维护 scope stack：

```python
scope = [
    root_scope,
    foreach_scope,
    nested_scope
]
```

遍历 `$var` 时查当前可见符号表。

这样：

```text
未定义变量
越界 capture
变量重复声明
foreach var 重名
```

全部在执行前发现。

我进一步建议 v1 **禁止 shadowing**：

```text
foreach freq
    foreach freq       reject
```

以及：

```text
capture v
capture v              reject in same scope
```

这能省掉很多模型和日志层面的歧义。

如果未来需要把循环结果送到外层，新增显式 aggregate 机制，例如：

```text
collect / reduce
```

再升级 DSL 版本，不给 v1 增加隐式变量提升。

---

## 3. `on_error: stop`：返回全部 partial state

你的倾向正确。

执行到：

```text
step 0 success
step 1 success
step 2 success
step 3 failure
```

返回必须明确表达：

```json
{
  "ok": false,
  "status": "stopped_on_error",
  "completed": false,
  "failed_step": "...",
  "leaf_results": [
    "... step 0 ...",
    "... step 1 ...",
    "... step 2 ...",
    "... step 3 failure ..."
  ],
  "capture_log": [
    "..."
  ],
  "warnings": [
    "..."
  ]
}
```

已经产生的结果绝不能丢。

仪器状态已经发生变化。例如：

```text
set 1 kHz     success
measure       success
set 2 kHz     success
measure       failed
```

此时隐藏前三步会让调用方失去判断当前设备状态所需的信息。

### capture 不建议返回成一个扁平 dict

因为 foreach 中：

```text
capture voltage
capture voltage
capture voltage
...
```

合法，并且属于不同 iteration scope。

建议返回事件日志：

```json
{
  "capture_log": [
    {
      "path": "/steps/0/iterations/0/steps/1",
      "bindings": {
        "freq_hz": 100
      },
      "var": "voltage_result",
      "value": {
        "...": "..."
      }
    },
    {
      "path": "/steps/0/iterations/1/steps/1",
      "bindings": {
        "freq_hz": 150
      },
      "var": "voltage_result",
      "value": {
        "...": "..."
      }
    }
  ]
}
```

这样没有作用域歧义。

对于 deadline 同样遵循这个原则：

```json
{
  "ok": false,
  "status": "deadline_reached",
  "completed": false,
  "leaf_results": [...],
  "capture_log": [...],
  "warnings": [...]
}
```

当前已经启动的 canonical operation 允许自然结束，记录它的最终结果，然后停止启动下一 step。

---

# 四、建议固定的验证流水线

实现时我建议严格按这个次序，后面不要再互换：

```text
1. JSON parse
2. plan.schema.json structural validation
3. plan_version check
4. registry lookup，确认所有 op 存在
5. lexical scope / variable binding validation
6. max_nesting_depth validation
7. exact max_leaf_steps cardinality calculation
8. server hard-limit validation
9. submit 整个 batch 为 ONE executor job
10. 每个 operation 开始前：
    a. execution deadline check
    b. resolve {"$var": "..."}
    c. FastMCP-derived operation schema validation
    d. invoke canonical operation
    e. parse JSON response
    f. capture result / JSON Pointer
    g. append leaf result / warning / audit data
11. batch 结束后 BUSY clear
```

这里有一个刻意保留的动态验证点：

```text
operation schema validation
```

需要在变量绑定后执行，因为：

```json
{"$var": "freq_hz"}
```

在 plan 层是合法引用，解析成：

```json
1000
```

以后才能验证它是否满足 operation 的真实 schema。

而 `op` 是否存在、变量是否在作用域内、leaf 总数、嵌套深度，都应该在仪器执行开始前一次性排干净。

这版已经可以直接作为 **Batch Plan DSL v1 的 schema + semantic validator contract**。memcite
