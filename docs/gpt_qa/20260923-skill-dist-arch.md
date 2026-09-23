# GPT 讨论：skill 分发与工具描述分层的架构评审（2026-09-23）

<!-- chatgpt-discuss: session=skill-dist-arch conv=https://chatgpt.com/c/6ab39d3b-b244-83ea-bb17-da315d5c5d3a updated=2026-09-23 17:42:21 rounds=3 -->
- 会话: skill-dist-arch | 链接: https://chatgpt.com/c/6ab39d3b-b244-83ea-bb17-da315d5c5d3a

## Q1 2026-09-23 17:38:23
**Q:** # 背景（自包含，请当作陌生项目看）

一个 Windows 上的仪器控制项目（Python + VISA/SCPI），通过一个 MCP 服务器把 **68 个工具**暴露给 AI 编码 agent。同一个项目同时被多个 AI 工具使用（DSH / Codex / ZCode / Claude Code），它们共享一个用户级 skill 目录。

两套机制的事实：

**skill 机制**：每个 skill 是 `<name>/SKILL.md`，其 YAML frontmatter 的 `description` 会**常驻注入每次请求**（形成一份 skill catalog），正文只在模型主动加载时才进上下文。该 agent 框架的 skill 文件监听器默认 `watchFollowSymlinks = true`（我核实过源码）。

**工具机制**：MCP 工具的 `description` 与参数 schema 同样**常驻**每次请求。实测一次请求的固定前缀为 **47,561 token**，其中工具 schema 占 **73%**（147 个工具）、skill catalog 占 13%（45 条）、常驻规则文件占 9%。

**一个关键约束**：所用 MCP 框架（FastMCP）没有使用参数级 `Field(description=...)`，因此**参数 schema 里只有 title 和 type，没有 description、没有 enum** —— 所有参数语义只能写在工具的 docstring（即 description）里。

---

# 待评审的两个决策

## 决策 A：skill 的单一权威源 + 目录链接分发

历史状态：同一份 SKILL.md 存在两份（一份在仓库内、一份在用户级 skill 目录），逐字节相同，靠人工同步。该项目此前在另一个 skill 上已因"双副本漂移"吃过亏。

新设计：

- 仓库 `skills/<name>/SKILL.md` 是唯一权威源
- `scripts/install_skills.py` 把它分发到 skill 根；**默认建立目录链接（Windows junction）而不是复制**
- 目标已存在且内容与源不一致 → **拒绝覆盖**（保护本地修改）；只有 `--force` 才覆盖，且先完整备份
- `--check` 只报告状态；`--uninstall` 只删"指向本仓的链接"或"逐字节一致的副本"，不碰有改动的目录

我的论据：复制仍然产生"两份实体"，只是把人工同步换成脚本同步，漂移风险降级但没消失；链接从根上消除漂移（改仓库即生效、无需重装），且因为 watcher 跟随符号链接，热更新不受影响。

## 决策 B：工具 description 与 skill 的职责分工

对 20 个最啰嗦的工具执行了如下分层：

- **工具 description（常驻）** = 能力一句话 + 安全/授权门 + 短枚举（≤8 项）+ 指向 skill 的定位锚点
- **skill（按需加载）** = 完整手册：长参数枚举（例如 51 个测量项的全列表）、手册页码、实测数值、返回体字段清单、背景原理

结果：这 20 个工具 description 的字符数 11,074 → 6,757（-39%）；全部工具 description 从 9,362 → 7,725 est token。

用脚本做了信息守恒核对：抽取"旧 description 减新 description"的 57 个**可机械识别信息原子**（SCPI 命令、文件路径、手册页码、数值与单位），逐一确认仍出现在"新 description 或 skill"中，**0 未命中**。

我的论据：

- 弱模型在长工具目录下倾向"想而不做"（项目内一份实验记录：极窄接口下工具调用率 100%，宽接口下约 25%）
- 参数枚举属于"调用前必须知道"，所以留在 description；手册页码与实测数值属于"查细节时才需要"，适合按需加载

---

# 我已知的反驳点（请补充与裁决）

1. **模型可能不读 skill 就直接调用**，参数填错 → 多轮失败重试。这是不是把"常驻上下文成本"换成了"运行时失败成本"？什么条件下这笔交易是亏的？
2. **skill 正文约 51 KB（≈12K token）**，一旦加载就进入对话历史并常驻。若一次会话里读两次以上，总账是否反而比原来更长？是否该给 skill 正文也做分层？
3. **链接的失败模式**：仓库目录被移动/改名/删除 → skill 断链；按路径复制的备份工具可能不跟随链接；跨卷、网络盘、同步盘（OneDrive 等）是否可靠？有没有更稳的等价方案？
4. **多机部署**：链接只适用于"仓库在本机"的场景。同一份 skill 要装到另一台机器时是否只能回到复制（于是又变成多源）？如何避免？
5. **常驻文本与按需文档的信息重复**：安全门这类内容（例如"开与关都需 confirm=True，关闭同样可能打断他人实验"）我在 description 和 skill 里**都保留了**。这种重复是否值得？常驻与按需两层的"信息重复"取舍，有没有可操作的判据？

---

# 请给出

1. 这两个决策各自的**反对意见**与**更优替代方案**（如果存在）
2. 我漏掉的失败模式
3. 对第 5 点：常驻 description 与按需 skill 之间的信息重复，给出可操作的判据
4. 如果只能保留一条改进，你选哪条，为什么


**A:**
结论先给：

- **决策 A 基本成立，但我会把“junction 是默认安装形态”改成“开发机默认链接，部署机默认受控复制”。**
- **决策 B 方向正确，但你现在的切分标准还不够严格。真正该按“调用前必需信息”和“调用失败代价”切，而不是简单按“长短、手册细节”切。**
- 如果只能保留一条改进，我选：**给所有工具 description 建立“最小可成功调用契约”并自动测试。** 这比继续压 token 或优化 skill 分发更重要。

## 1. 决策 A：单一权威源 + junction

### 我赞成的部分

“仓库内唯一权威源”是对的。

你现在真正解决的是两个问题：

1. **authoring authority 唯一**
2. **部署副本不会静默覆盖用户修改**

`--check / --force + backup / --uninstall` 的语义也比较完整。

但我反对把：

> “复制有第二份实体，所以复制天然仍然是多源”

作为主要论据。

**实体有几份和权威源有几个不是一回事。**

例如：

```text
repo/skills/foo/SKILL.md        authoritative
~/.skills/foo/SKILL.md          generated artifact
```

只要后者被严格定义为 generated artifact，禁止人工编辑，并且：

```text
install
check
CI hash verification
```

都以 repo 为源，那么架构上仍然只有一个 source of truth。

所以 junction 的核心收益其实是：

> **开发态即时一致性**

而不是“只有链接才能实现单一权威源”。

---

### junction 我看到的主要问题

#### 1. 你把“安装”变成了“引用工作树”

这是 junction 最大的语义变化。

原本：

```text
install v1 skill
```

之后仓库随便切 branch、reset、checkout，已安装 skill 不变。

junction 后：

```text
git checkout experimental
```

用户级 skill **同步切到了 experimental 版本**。

甚至：

```text
git checkout old-release
git bisect
git stash
临时修改 SKILL.md
```

都会立即改变所有 DSH / Codex / ZCode / Claude Code 看到的 skill。

这实际上是：

> user-global runtime configuration 直接绑定当前 Git worktree mutable state。

这是我认为 A 中最大的遗漏。

你原本把“无需重新安装，仓库修改立即生效”当收益；它同时意味着：

> **任何未提交、半编辑、错误 branch 上的内容也立即生效。**

如果四个 agent 同时共享这个目录，blast radius 还会扩大。

---

#### 2. 原子性变差

复制安装可以这样做：

```text
生成 temp
校验
rename / replace
```

用户始终看到完整版本。

junction 指向工作树时，skill 文件可能处于编辑中：

```text
write truncate
write first half
write second half
```

watcher 恰好触发，就可能读取半成品。

IDE 多数时候会临时文件 + rename，但你不能把这个作为架构保证。

如果 SKILL.md 有 YAML frontmatter，半写状态甚至可能导致整个 skill catalog 解析异常。

---

#### 3. repository lifecycle 泄漏到了 runtime

你已经提到：

- repo 移动
- 删除
- 改名

我再加：

- `git worktree`
- branch switch
- detached HEAD
- sparse checkout
- repository cleanup
- 临时 clone
- CI checkout
- developer 修改 skill 做实验
- 一台机器存在同项目两个 clone

尤其是最后一点。

假设：

```text
D:\src\instrument
D:\src\instrument-test
```

两个仓库都是合法 clone。

哪个 clone 应该控制用户级 skill？

junction 强制把“source identity”退化成了“某个本机路径”。

---

### Windows junction 还有一个值得注意的点

如果你实际建立的是 **directory junction**，建议命名和实现里明确写 `junction`，不要泛称“symlink”。

原因是它的：

- 权限要求
- 跨卷行为
- 网络路径行为
- 删除行为
- Python API 识别行为

和真正的 NT symbolic link 并不完全一致。

特别要测试：

```python
Path.is_symlink()
os.path.islink()
os.stat()
os.lstat()
shutil.rmtree()
```

你的 `--uninstall` 千万不要依赖“is_symlink() == True”来判断 junction。

这类错误有机会把目标目录递归删掉，风险比 skill 漂移大得多。

---

## 我对 A 的替代方案

我会定义两种明确模式：

```text
install --mode=link
install --mode=copy
```

其中：

```text
link = developer mode
copy = deployment mode
```

默认策略可以是：

```text
本机 repo 开发环境    link
跨机 / OneDrive / 网络盘 / CI / 发布环境    copy
```

复制模式也不应该是“裸复制”，而应带 provenance：

```text
.skill-origin.json
```

例如：

```json
{
  "source": "instrument-control",
  "skill": "rigol-control",
  "revision": "git-sha256...",
  "content_sha256": "...",
  "install_mode": "copy"
}
```

于是 `--check` 能准确说：

```text
CURRENT
OUTDATED
LOCALLY_MODIFIED
SOURCE_MISSING
WRONG_SOURCE
BROKEN_LINK
```

这样多机也完全不需要“多源”。

另一台机器：

```text
git clone authoritative repo
install_skills.py --mode copy
```

权威源依旧是 Git 仓库。

**deployment copy ≠ authority copy。**

---

# 2. 决策 B：description / skill 分层

总体上，我赞成。

47,561 token 固定前缀里，工具 schema 73%，skill catalog 13%，你优化工具 description 是在攻击真正的大头。

但我认为你当前有一个潜在错误：

> “短枚举留常驻，长枚举进 skill”

**长度不应是主要边界。**

真正的边界应是：

> **模型在不读取 skill 的情况下，能否一次构造出合法且安全的调用。**

---

## 什么必须留在 description

我会把 description 定义为：

> **Minimal Successful Invocation Contract，MSIC**

至少包含四类。

### A. 选择工具所需

模型看到 catalog 后必须知道：

```text
这个工具什么时候用
和相邻工具有什么区别
```

例如：

```text
read_measurement
configure_measurement
stream_measurement
```

如果区分条件只在 skill，模型连“该不该读 skill”都无法判断。

---

### B. 构造合法调用所需

包括：

```text
参数语义
单位
取值域
条件依赖
互斥关系
默认值陷阱
关键枚举
```

这里有一点我会反驳你：

> **51 个合法 measurement 项如果调用时必须精确选择，它们即使很长，也不天然适合移进 skill。**

因为你的 FastMCP schema 没有 enum。

如果模型必须写：

```python
measurement="VOLT_DC"
```

而 schema 只告诉它：

```json
{"type":"string"}
```

description 又不列完整允许值，那么工具接口事实上变成：

```text
stringly typed undocumented API
```

这会直接制造 hallucinated argument。

这时应该考虑的替代方案是改工具 API，而不是单纯搬文档。

例如：

```text
measurement="voltage"
mode="dc"
```

或者：

```text
measurement_id
```

配套：

```text
list_measurements()
```

或者直接在 Pydantic/FastMCP 层恢复 Enum/Literal，让 schema 自己表达。

如果框架当前封装层不支持，你甚至值得为这件事改 wrapper。

**减少 1K token 不值得拿调用正确率交换。**

---

### C. 安全前置条件

必须留。

例如：

```text
requires confirm=true
interrupts active acquisition
writes persistent calibration
reboots instrument
```

因为这些信息必须在“是否调用”的决策时可见。

skill 只能做扩展解释。

---

### D. 返回结果里决定下一步的信息

例如：

```text
returns job_id, use get_job_status next
```

或者：

```text
returns raw binary path, not measurement value
```

如果模型只有调用后才能知道如何解释结果，描述里应该留一句。

---

# 3. “模型不读 skill 就调用”何时会让这笔交易亏掉

可以直接用一个成本模型。

假设：

```text
C_static = 每轮常驻 token 成本
P_need   = 本轮需要该细节的概率
C_skill  = 加载 skill 后增加的上下文成本
P_fail   = 未读 skill 导致失败概率
C_retry  = 一次失败后的额外成本
R        = 后续对话轮数
```

搬进 skill 大致有利于：

```text
R * C_static_saved
>
P_need * C_skill_lifetime
+ P_fail * C_retry
```

这里最容易被忽略的是：

> skill 一旦加载后通常会污染整个后续 transcript。

所以 `C_skill_lifetime` 并不只是 12K：

```text
12K × 后续被反复送入模型的轮数
```

虽然具体平台可能有 prompt cache，逻辑上下文成本仍然存在。

因此下面几种情况搬进去通常是亏的：

1. **调用频率高**
2. **该信息几乎每次调用都需要**
3. **参数猜错会触发设备动作**
4. **失败代价高**
5. **重试昂贵，例如仪器 I/O 5~30 秒**
6. **工具不可逆或有物理副作用**
7. **模型没有稳定的“先读 skill”行为**
8. **会话通常很长，skill 一加载就长期驻留**

反过来，适合 skill 的是：

```text
低频
解释型
诊断型
reference 型
调用前通常不需要
错误后也能根据错误消息恢复
```

例如：

- 手册页码
- SCPI 原理
- 实测 latency
- firmware 差异
- 返回 JSON 每个冷门字段的解释

这些你的判断是对的。

---

# 4. 51 KB / 12K token 的 skill 要不要再分层

**要。**

12K token 的单 skill 已经足够大，我不会把它视为“轻量按需文档”。

尤其对 agent 而言：

```text
load skill
```

可能只是为了查一个 measurement 枚举，却一次灌入 12K。

我更建议：

```text
SKILL.md
references/
    measurements.md
    configuration.md
    acquisition.md
    safety.md
    return-formats.md
    device-notes.md
```

其中 `SKILL.md` 只做：

```text
1~2K token router + 核心契约 + 文档索引
```

正文中明确：

```text
Measurement names → references/measurements.md
Return fields → references/return-formats.md
```

但这里有一个边界：

**不要无限递归拆 skill。**

否则模型变成：

```text
catalog
→ SKILL
→ index
→ references
→ another reference
```

你又制造了工具发现开销。

我认为合理深度就是两层：

```text
catalog
→ SKILL.md
→ one reference file
```

最多如此。

---

# 5. 你漏掉的 B 类失败模式

## ① skill 定位锚点本身可能无效

description 写：

```text
See skill X for details.
```

你需要验证所有 agent：

- 是否认识这个 skill name
- 是否能主动加载
- 是否同名冲突
- skill 缺失时怎么行为

否则这句话只是给模型一个不可执行建议。

---

## ② skill 与工具版本失配

MCP server 是版本 A：

```text
tool expects foo=...
```

用户级 skill 是版本 B。

junction 在单仓本机上降低这个风险；跨机 copy 又会重新出现。

所以 skill 文档里最好带：

```text
tool API version
```

或者由服务暴露：

```text
get_server_info()
```

至少让诊断时能识别 mismatch。

---

## ③ description→skill 的信息搬迁可能破坏 tool selection

你的“57 个信息原子 0 丢失”验证只能证明：

> textual preservation

不能证明：

> behavioral preservation

例如旧 description：

```text
Use this tool for waveform memory, not screenshot data.
```

新 skill 中仍然有这句话。

机械检查通过。

但 tool catalog 阶段模型看不到它，于是选错工具。

所以还应该做另一类验证：

> **routing information conservation**

测试一组 prompts：

```text
用户意图 → 正确 tool
```

在 old/new catalog 上分别跑。

这是你当前检查体系最大的缺口。

---

## ④ 参数错误消息质量会决定分层是否可行

如果模型传错：

```text
measurement="FREQENCY"
```

工具返回：

```text
invalid argument
```

那运行时恢复很差。

如果返回：

```text
Unknown measurement "FREQENCY".
Closest values: FREQUENCY, PERIOD.
Call list_measurements for all supported values.
```

你就可以更激进地缩 description。

所以：

> **description 可以多瘦，取决于 runtime validation 有多强。**

这一点通常比文档本身更关键。

---

## ⑤ catalog 本身存在同名 / 语义近邻压力

68 个本项目工具，加上总计 147 个工具，问题已经不只是 description token。

模型还有：

```text
tool discrimination complexity
```

即使每个 description 都只有一句话，147 个候选也会降低 selection reliability。

所以你后续最大的收益可能来自：

```text
工具动态暴露
工具分组
工具路由器
按设备 namespace 选择
```

而不是继续从 7,725 token 压到 6,000。

这与“窄接口 100%，宽接口约 25%”的实验现象是高度一致的。

---

# 6. 第 5 点：常驻与按需之间何时允许重复

我建议采用一个很明确的判据：

> **如果遗漏该信息会导致错误工具选择、非法调用、危险副作用、不可逆状态变化，或无法恢复的上下文损失，就允许重复。**

换成表：

| 信息 | description | skill |
|---|---:|---:|
| 危险动作 / confirm | 必须 | 建议重复并展开 |
| 会中断当前实验 | 必须 | 建议重复 |
| 参数单位 | 必须 | 可重复 |
| 参数合法范围 | 必须 | 可展开 |
| 高频枚举 | 必须 | 可重复 |
| 冷门长枚举 | 视 API 而定 | 适合 |
| 工具选择边界 | 必须 | 可展开 |
| 手册页码 | 否 | 是 |
| 实测性能数据 | 否 | 是 |
| 背景原理 | 否 | 是 |
| 完整返回字段字典 | 通常否 | 是 |
| 故障排查流程 | 一句入口 | 是 |

你的例子：

> 开与关都需 `confirm=True`，关闭同样可能打断他人实验

我会**明确重复**。

description：

```text
Both enable and disable require confirm=true; disabling may interrupt an active experiment.
```

skill：

```text
为什么如此
影响哪些状态
如何确认没有其他 session
失败恢复步骤
```

这不属于坏重复。

这是：

> **policy duplication with different granularity**

---

## 一个更机械的判断方法

对每条信息问三个问题：

### Q1
模型在调用工具**之前**必须知道吗？

是：

```text
description
```

### Q2
不知道它会造成物理、安全、状态或高成本失败吗？

是：

```text
description + skill 重复
```

### Q3
它只是帮助理解、诊断、优化或查证吗？

是：

```text
skill only
```

这个规则足够适合直接写进项目规范。

---

# 7. 对 A、B 各自的最终裁决

### A

我会保留：

```text
仓库唯一权威源
check
force + backup
safe uninstall
```

我会修改：

```text
junction 默认
```

改成：

```text
developer link mode
deployment copy mode
```

并给 copy 添加：

```text
source revision + content hash + origin metadata
```

这比“任何地方都 junction”稳。

---

### B

保留方向。

但把当前：

```text
短的留 description
长的进 skill
```

升级为：

```text
description = Minimal Successful Invocation Contract
skill = extended reference
```

然后给每个工具自动检查：

```text
tool selection 信息是否仍常驻
构造合法调用的信息是否仍常驻
所有 safety gate 是否仍常驻
```

你现在的 57 个信息原子核对可以继续保留，但它只是 **content-loss test**，还缺 **behavior-loss test**。

---

# 8. 如果只能保留一条改进

我选：

> **为 description 定义并自动验证“最小可成功调用契约 MSIC”。**

原因很简单。

A 出问题主要会造成：

```text
skill 版本漂移
断链
部署异常
```

这些通常是可检测、可恢复的配置问题。

B 切错边界会直接造成：

```text
模型选错工具
参数 hallucination
反复重试
危险调用前缺信息
```

而且你的环境已经有 **147 个工具 + 无参数 description/enum 的 schema**。这里本来就是整个接口里最脆弱的一层。

因此我会优先保证：

```text
模型仅凭常驻 catalog
就能选对工具
并构造一次合法、安全的调用
```

做到这一点以后，再尽量把其余材料卸载到 skill。

这条约束比“description 要短”重要得多。

## Q2 2026-09-23 17:40:22
**Q:** # 先报实测结果（针对你那条 junction 删除警告）

你的警告方向完全正确，而且实测抓住了它最阴险的一面。我写了一个 30 断言的离线套件把这条路径钉死，其中一条 INFO 输出是：

```
Path.is_symlink()=False   os.path.islink()=False   is_reparse_point()=True
```

也就是说：**Windows junction 在 CPython 上 `is_symlink()` 返回 False**。我的 `--uninstall` 里原本有一处 `os.rmdir(dst) if not dst.is_symlink() else dst.unlink()` —— 它在 junction 上恰好走对了分支，但那是靠"这几个 API 恰好都不认 junction"的巧合，不是设计。

按你的建议已改为不依赖任何判定：新增 `remove_link()`，先 `os.rmdir`（对目录链接只摘链接、不递归），失败再回退 `unlink`。套件覆盖：链接安装→卸载后**源目录逐字节 + mtime 均未变**、copy 模式卸载、被本地改过的副本拒删、指向别处的陌生链接拒删。当前 30 PASS。

**"复制仍是单源（generated artifact）"这个纠正我接受** —— 我原来的论据（"复制天然多源"）不严谨，实体份数 ≠ 权威源个数。

---

# 但有一点我要提出反论，请你裁决

你反对 junction 的核心论据是：它把"安装"变成了"引用工作树"，于是 `git checkout experimental` / `git stash` / 半编辑的 SKILL.md 会**立即**影响所有 agent。

这个事实我承认。但请评估我在**这个具体约束**下的取舍：

- 单机、单仓、这台机器就是开发机（不是部署机），skill 与代码由同一个人在同一次工作时修改
- 我真正想消灭的故障是：**代码改了、忘了重装 skill，于是 agent 读到的是旧版 SKILL.md**（这个项目在另一个 skill 上正是因双副本漂移出过事）
- 反过来，"仓库切到实验分支导致 skill 跟着变"在本机场景下**恰恰是我要的**：skill 就该与代码同版本

所以：在"单机开发"这个约束下，你认为 link-默认是否仍然错误？还是说你的建议实质是"两种模式都提供、由使用者显式声明"（这点我已接受，正在加 `--mode` 与 provenance 元数据）？

---

# 需要你给可操作做法的四个问题

## 1. MSIC 怎么变成自动检查

你把"为 description 定义并自动验证最小可成功调用契约"列为唯一该保留的改进。但"必须常驻的信息"里只有一部分机械可判（参数名、单位、危险动作关键词、枚举是否存在），而**"工具选择边界"是语义信息**，怎么自动验证而不跑 LLM？

我自己只能想到弱版本：为每个工具维护一份"必须常驻项"清单（关键词/正则），脚本断言 description 命中。这能防"改写时漏抄"，防不了"边界写得含糊"。有更强的做法吗？

## 2. routing information conservation 的低成本替代

你说这是我检查体系最大的缺口——我同意，我的 57 原子核对只证明 textual preservation。但完整做法（old/new catalog 各跑一组 prompt 看工具是否选对）要跑 LLM、成本高。**有没有不跑 LLM 的替代？** 我想到的是列出"语义近邻工具对"（例如 measure vs meas_statistics、channel 设置 vs 单通道自动定标），逐个核对 description 是否给出了互斥判据。这算够用吗？

## 3. 长枚举：三选一的判据

你反对把 51 个测量项移进 skill，理由是 schema 没有 enum → 制造 hallucinated argument。这里补一个你不知道的事实：**我的框架支持让 schema 自带 enum**（Python + Pydantic `Literal`/`Enum`，或 `Annotated[str, Field(description=...)]`），只是这个项目当初没这么写。

于是同一件事有三种做法：

- ① 枚举留在 description（现状，常驻）
- ② 改成 `Literal[...]` 让 **schema 自带 enum**（常驻 token 与 ① 基本相当，但结构化、可被客户端校验、模型看到的是合法值清单）
- ③ 加一个 `list_measurements()` 工具，枚举按需查（省常驻 token，但多一次调用；对高频测量是负担）

我倾向 ②（token 不变但正确率提升），③ 只用于低频长枚举。请给判据：**什么时候值得用 ③ 换 token，什么时候必须用 ② 甚至 ① 保正确率**？

## 4. skill 两层怎么切

我的 SKILL.md 是 51 KB（≈12K token），你建议切成 `SKILL.md`（1–2K router + 核心契约 + 索引）+ `references/*.md`，且最多两层，我接受。但有个约束：我的框架里 skill 正文是**整篇注入**的，`references/` 需要模型自己再发一次读文件动作。请给切分判据：哪些内容**必须**留在 SKILL.md 正文、哪些可以下沉，才能避免"读了 SKILL.md 还得再读一个文件才知道该读哪个"？

---

# 最后一个优先级裁决

你选"MSIC 自动验证"为唯一改进。但我这边还有一个更大的杠杆候选：**把工具总数从 147 降到 46** —— 方法是不再让仪器类工具全局注册，改为按项目/会话预设按需挂载，于是单会话只看得见当前项目需要的工具。你自己的第 ⑤ 点也提到"工具动态暴露 / 分组 / 路由器"的收益可能大于继续压 token；而本项目有一份实测：极窄接口下工具调用率 100%，宽接口下约 25%。

**请裁决**：MSIC 自动化 vs 工具数 147→46，哪个优先？若都要做，顺序是什么？


**A:**（⚠ **本轮记录无效**：脚本读取回答时连续两次遇到 `HTTP 429`，回退写入了**上一轮回答的副本**。这不是 GPT 对本轮的回答。本轮真正的问题已送达，其回答见下方 **Q3**。）

---
## Q3 2026-09-23 17:42:21
**Q:** 请回答我上一条消息（标题为「先报实测结果（针对你那条 junction 删除警告）」）提出的四问与最后裁决。为避免依赖上下文，这里复述要点：

**1. MSIC 怎么变成自动检查？** 你说要把 description 定义为"最小可成功调用契约"并自动验证。但其中"工具选择边界"是语义信息，不跑 LLM 怎么验证？我只能想到弱版本（维护关键词/正则清单，断言 description 命中），它只能防漏抄、防不了边界写含糊。有更强的做法吗？

**2. routing information conservation 的低成本替代。** 你说这是我检查体系最大的缺口，我同意。但完整做法要跑 LLM 生成 prompt 再比对工具选择，成本高。我想到的替代是：列出**语义近邻工具对**（例如 measure vs meas_statistics、channel 设置 vs 单通道自动定标、query vs write），逐个核对 description 是否给出了**互斥判据**。这够用吗？还是必须跑行为测试？

**3. 长枚举三选一的判据。** 你反对把 51 个测量项移进 skill（schema 无 enum → 制造幻觉参数）。补充一个事实：**我的框架支持让 schema 自带 enum**（Python + Pydantic `Literal`/`Enum`，或 `Annotated[str, Field(description=...)]`），只是项目当初没这么写。于是有三种做法：① 枚举留在 description；② 改用 `Literal` 让 schema 自带 enum（常驻 token 与①相当，但结构化、可校验、模型看到合法值清单）；③ 加 `list_measurements()` 工具按需查（省常驻 token，但多一次调用）。请给判据：**什么时候值得用③换 token，什么时候必须用①或②保正确率**？

**4. skill 两层切分的判据。** 我的 SKILL.md 是 51 KB（≈12K token），你建议切成 `SKILL.md`（1–2K router + 核心契约 + 索引）+ `references/*.md`，最多两层，我接受。但有个约束：**我的框架里 skill 正文是整篇注入的**，`references/` 需要模型自己再发一次读文件动作。请给切分判据：哪些内容**必须**留在 SKILL.md 正文、哪些可以下沉，才能避免"读了 SKILL.md 还得再读一个文件才知道该读哪个"？

**5. 最后裁决。** 你选"MSIC 自动验证"为唯一改进。但我这边还有更大的杠杆候选：**把工具总数从 147 降到 46**（方法：仪器类工具不再全局注册，改为按项目/会话预设按需挂载，单会话只看得见当前项目需要的工具）。你自己的第 ⑤ 点也提到"工具动态暴露/分组/路由器"收益可能大于继续压 token，且本项目有一份实测：极窄接口下工具调用率 100%，宽接口下约 25%。**请裁决**：MSIC 自动化 vs 工具数 147→46，哪个优先？若都做，顺序是什么？

另外请一并裁决这个取舍：你担心 junction 把"安装"变成"引用工作树"（切分支/半编辑会立即影响所有 agent）。但在**单机、单仓、同一开发者**的场景下，我认为这正是我要的（skill 与代码同版本），我真正要防的故障是"改了代码、忘了重装 skill 导致 agent 读到旧文档"。请判断：在这个约束下 link-默认是否仍然错误？


**A:**
先裁决你对 junction 的反论：**在你给出的单机、单仓、开发机、代码与 skill 同步演进的约束下，link 默认是合理的。**

我上一轮反对的是把 link 当成“普适安装语义”。你的具体目标是让 skill 与当前工作树版本强绑定，这时 branch switch、stash、实验修改同步生效，属于预期行为。

我建议最后把语义写死：

```text
--mode=link    开发绑定模式：skill 始终跟随当前工作树
--mode=copy    部署快照模式：skill 固定为安装时版本
```

若脚本能识别“源在本机 Git 工作树”，默认 `link` 可以接受。跨机、CI、同步盘、发布环境默认 `copy`。关键是名称和帮助文本要明确表达这种生命周期差异。

你现在的 `remove_link()` + 30 断言测试已经覆盖了 junction 删除中最危险的一类故障。再补一条即可：**源目录不存在的 broken junction 也必须能安全卸载**，且仍要验证它确实由本安装器创建或 provenance 匹配。

---

# 1. MSIC 怎么自动检查

你判断得对：**MSIC 无法完全靠静态脚本验证语义质量。** 不跑 LLM 时，目标应该降级成：

> 自动验证“契约完整性”，人工评审“契约清晰度”。

不要试图让 regex 判断一句话是否“足够清楚”。

我建议给每个工具增加结构化的 companion manifest，不要只维护关键词：

```yaml
tool: set_channel_scale

selection:
  purpose: "设置指定通道的垂直档位"
  excludes:
    - "自动寻找合适档位"
    - "读取当前档位"
  neighbors:
    - auto_scale_channel
    - get_channel_scale

arguments:
  channel:
    required_semantics:
      - "合法通道范围"
  scale:
    required_semantics:
      - "单位"
      - "合法范围"

safety:
  - "may_change_acquisition"
  - "confirm_required"

returns:
  - "new_scale"
```

然后做三层检查。

### 第一层：100% 机械

检查 schema 和 description 的一致性：

- 所有参数都在 description 或 schema 中有语义来源
- `Enum/Literal` 真正在 schema 中出现
- safety manifest 非空时 description 必须出现对应规范句
- neighbor/excludes 必须在 description 中有区分信息
- description 引用的 skill 名必须实际存在
- skill 中引用的工具名必须实际存在

这层可以 CI 强制。

### 第二层：半结构化 lint

不要查任意关键词，查**规范句型或标记**。

例如统一成：

```text
Use when: ...
Do not use for: ...
Requires: ...
Values: ...
Returns: ...
See skill: ...
```

这样脚本可以检查：

```text
存在 neighbors -> 必须有 Do not use for
dangerous -> 必须有 Requires
长枚举下沉 -> 必须有 See skill 或 schema enum
```

这比“description 中是否包含 `confirm`”强很多。

### 第三层：人工语义审查

只审两个字段：

```text
Use when
Do not use for
```

也就是真正难自动化的 routing boundary。

所以我会把上一轮的“MSIC 自动验证”修正得更精确：

> **MSIC 应当结构化，并自动检查其完整性与 description/schema 的映射；语义区分度仍需人工或周期性模型测试。**

这是能落地的版本。

---

# 2. routing information conservation 不跑 LLM 怎么做

你提出的“语义近邻工具对”是正确方向，而且我认为**足够作为日常低成本防线**。

但我建议不要只维护 pair，改成 **confusion set**。

例如：

```yaml
group: channel_vertical
tools:
  - set_channel_scale
  - auto_scale_channel
  - get_channel_scale

distinguishing_axes:
  operation:
    set_channel_scale: explicit-write
    auto_scale_channel: automatic-search
    get_channel_scale: read-only

  scope:
    set_channel_scale: one-channel
    auto_scale_channel: one-channel
    get_channel_scale: one-channel
```

另一个：

```yaml
group: measurement
tools:
  - measure
  - meas_statistics
  - acquire_waveform

distinguishing_axes:
  result:
    measure: scalar
    meas_statistics: aggregate-statistics
    acquire_waveform: waveform-data
```

CI 可以检查：

1. confusion set 内每个工具都有至少一个唯一 discriminant；
2. discriminant 必须映射到 description 的固定字段；
3. 新增同组工具时，没有 discriminant 就拒绝合入；
4. 删除或修改 description 时，不能使两个工具的 routing signature 相同。

这解决的是：

> **structural routing conservation**

它依然不能证明模型一定选对，但已经能捕捉大量真实退化。

我会采用三档策略：

- 每次提交：静态 confusion-set lint
- description 大改：人工 review
- 发布前或接口重大调整：跑一次小规模 LLM routing regression

没有必要每次 CI 都付模型成本。

---

# 3. 51 个枚举，② / ③ / ① 怎么选

你的倾向基本正确。

优先级通常应是：

```text
② schema Enum/Literal
> ① prose description
> ③ list_* runtime discovery
```

这里的核心维度有四个：

### A. 调用频率

高频：

```text
必须优先 ②
```

原因是避免每次先发现再调用。

低频：

```text
③ 才开始有价值
```

---

### B. 枚举稳定性

稳定、版本变化少：

```text
② 很适合
```

因为静态 schema 是真契约。

动态变化，例如不同仪器型号、固件、插件决定可用项：

```text
③ 更合理
```

否则 schema 很快失真。

---

### C. 枚举规模

我不会只按“51 个”机械切。

大致可以这样：

```text
<= 10        几乎总是 ②
10~50        仍优先 ②
50~200       看调用频率和稳定性
几百到几千   倾向 ③
```

51 个 measurement 并没有大到足以单独成为 ③ 的理由。

---

### D. 错值的失败代价

如果错值只会返回：

```text
invalid measurement
```

③ 可以接受。

如果错误值可能映射到合法但错误的模式，或者触发真实仪器状态：

```text
必须 ②
```

---

## ① 什么时候还有存在价值

当值域无法方便编码进 schema，但有几条对模型非常重要的别名/常用值时。

例如 schema 是开放字符串：

```text
measurement: str
```

但你可以在 description 中留：

```text
Common values: DCV, ACV, FREQ.
Full device-dependent list: use list_measurements().
```

所以 ① 更适合作为 **human/model guidance**，不应承担 primary validation。

对你的 51 项，我会直接改成 **② Literal/Enum**。

如果不同设备支持子集，则进一步变成：

```text
静态 Enum = 协议全集
运行时 validation = 当前设备子集
```

或者：

```text
list_measurements() = 当前设备 capability discovery
```

两者并存也完全合理。

---

# 4. 12K skill 两层怎么切

你的约束意味着 `SKILL.md` 必须做到一件事：

> **模型读完它以后，能够决定“现在是否已经足够行动；若不够，只需知道唯一该读哪个 reference”。**

所以正文里应该保留四类内容。

## 必须留在 SKILL.md

### 1. 能力地图

例如：

```text
测量
配置
波形
校准
设备状态
危险操作
```

让模型知道这个 skill 覆盖什么。

### 2. 全局契约

跨多个工具重复生效的东西：

- session / resource 约定
- confirm 规则
- read/write 副作用
- 设备选择方式
- 单位约定
- timeout / retry 总原则
- 哪些状态会被修改
- 高频错误处理

这些不能逼模型先猜 reference。

### 3. 高频调用路径

例如：

```text
普通标量测量：
1. ...
2. ...
3. ...
```

以及最常用参数。

如果 80% 的使用场景都要查 reference，router 太薄了。

### 4. 明确的一跳索引

不要写：

```text
More details are in references.
```

要写：

```text
完整 measurement 枚举 -> references/measurements.md
返回字段定义 -> references/returns.md
校准流程 -> references/calibration.md
故障诊断 -> references/troubleshooting.md
```

模型必须做到“看到问题即可唯一映射文件”。

---

## 可以下沉

满足下面任一条件即可：

- 低频
- 长列表
- 查证型
- 诊断型
- 不影响第一次正确调用
- 只影响解释深度
- 某个子领域独占

典型：

```text
51 项完整 measurement 说明
SCPI 映射表
手册页码
固件差异表
返回字段全字典
完整 error code 表
实测 latency
底层实现背景
```

---

## 一个很实用的切分测试

对 SKILL.md 中每一段问：

> 删除这一段后，模型是否还能正确决定下一步是“直接调用工具”还是“读取哪一个且唯一的 reference 文件”？

如果答案是“不能”，保留。

如果答案是“能，只是少了细节”，下沉。

这个判据比“正文控制到 2K token”更重要。

我会允许 2K 到 4K，不必为了数字强行压到 1K。

---

# 5. MSIC 自动化 vs 147 → 46

这里我改变上一轮优先级。

**147 → 46 优先。**

你的新增事实已经足够强：

```text
工具数量减少约 69%
```

而且有项目内实测：

```text
窄接口工具调用率 100%
宽接口约 25%
```

这意味着你现在面对的首先是 **candidate-set overload**，其次才是单个工具 description 质量问题。

两者作用层级不同：

```text
动态挂载
↓
减少模型必须区分的候选工具

MSIC
↓
提升剩余候选工具之间的可辨识性和调用正确率
```

前者改变搜索空间规模，通常杠杆更大。

---

## 我建议的顺序

### 第 1 步：147 → 46

先按项目 / 会话 preset 挂载。

目标不是极端最少，而是：

```text
只暴露当前任务有现实概率使用的工具
```

同时保留少量跨项目基础工具。

这里要防一个反作用：

> preset 切得太窄，模型发现不了它其实需要另一个工具。

所以最好有：

```text
当前 preset
+ 一个低成本 capability discovery / switch mechanism
```

具体不一定是工具，也可以由 host 配置处理。

---

### 第 2 步：把剩余 46 个工具 schema 做结构化

优先修：

```text
Literal / Enum
Field(description)
required constraints
```

因为这直接提高机器可验证性。

---

### 第 3 步：MSIC manifest + lint

此时做：

```text
Use when
Do not use for
Safety
Argument semantics
Returns
```

并建立 confusion sets。

---

### 第 4 步：再压 description token

最后才做这种优化：

```text
7,725 -> 6,xxx token
```

因为前面三步会改变最优文案结构。

---

# 最终优先级

我会排成：

1. **工具可见集 147 → 46**
2. **schema 结构化，尤其 Enum/Literal / 参数说明**
3. **MSIC manifest + confusion-set 静态回归**
4. **skill 两层化**
5. **继续削 description token**

理由很明确：

**先减少模型要做的选择数量，再提高每个选择的可辨识性，最后才优化上下文体积。**

你目前最大的系统性问题已经不像“description 太长”，更像：

```text
过宽的 action space
+ 弱 schema
+ 长常驻上下文
```

其中第一个是当前杠杆最大的。

> ⚠️ **本条回答被中途截断**（接口 is_complete=false / finish_details.type≠stop）—— 建议在同一会话发「继续」补全
