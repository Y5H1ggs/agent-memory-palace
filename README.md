# memory-palace

> 一个会遗忘、会被情绪加固、会讲自己故事的记忆系统。
> 零第三方依赖的 Python 参考实现。

[![CI](https://github.com/Y5H1ggs/memory-palace/actions/workflows/ci.yml/badge.svg)](https://github.com/Y5H1ggs/memory-palace/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)

**TL;DR (English)** — A dependency-free Python reference implementation of a *memory lifecycle*: idempotent decay, affect that both hardens and distorts recall, consolidation with a one-way door, value-based forgetting with a fixed attention budget, self-narrative as a projection (not a node), a four-layer persona, and self-correction bounded by a whitelist, value ranges and anti-lag verification. Ships an MCP server, an HTTP+OpenAPI shim and a CLI so any agent can inject a budgeted memory block. 10.5k lines, 251 assertions, 18 mutation audits. Not a vector database; not production-ready.

---

## 它解决的不是「存不下」，是「记不住该记的」

向量库解决的是「给我最相似的 k 条」。但一个真实的记忆系统要回答的是另外几个问题：

| 问题 | 向量库 | 这里 |
|---|---|---|
| 三个月没想起的事，该不该还在？ | 在，除非你删 | 按强度 + 价值判，还会随叙事地位退潮 |
| 情绪强烈的事是不是更难忘？代价是什么？ | 不知道 | 是，而且**越情绪化越可能是错的** |
| 十条碎片什么时候变成一条结论？ | 不会 | 结构 + 语义双通道结晶，单向门 |
| 我「选择」忘掉什么，凭什么？ | 不涉及 | 四分量重要性 + 审计理由 + 固定预算 |
| 「我是谁」是图里的一个节点吗？ | 是 | 不是。自述是读图的方式，是投影 |
| 系统能不能自己调参？调到什么程度必须停？ | 不涉及 | 12 个白名单参数，超范围只能上报给人 |

一句话：**这里建模的是记忆的生命周期，不是检索的一次调用。**

## 30 秒上手

```bash
git clone https://github.com/Y5H1ggs/memory-palace.git
cd memory-palace
python examples/quickstart.py     # 不装包也能跑
python -m memory_palace.demo      # 完整剧本 + 215 项自测（约 0.9 秒）
```

```python
from memory_palace import MemoryPalace, MutableClock

palace = MemoryPalace(clock=MutableClock())
palace.remember("loss:0914", "追高之后逆板块阴跌，亏了三个点，很后悔", tags=("复盘",))
palace.remember("kelly", "半凯利仓位：单笔不超过总资金三分之一", tags=("仓位",))

palace.recall(query="仓位该怎么分配")        # → [kelly]：标签零重叠也命中
palace.graph.get("loss:0914").affect         # 情绪由文本推断，带依据
palace.entropy()["structure_health"]         # 价值健康 ≠ 结构健康
palace.run_cycle()                           # 沉淀→抓取→遗忘→自述→人格→反思→自修正
```

`examples/quickstart.py` 的真实输出：

```
语义召回: ['kelly']
情绪档案: 效价 -0.46 / 唤醒 0.70 / 来源 text / 依据 ('亏', '后悔')
价值健康: healthy / 结构: isolated
多义词裁决: {'资金': {'position': 0.0652, 'sector_flow': 0.9348}}
最近遗忘判决: [('rule:no-absolute', 'kept', 'pinned_by_user'), ('loss:0914', 'kept', 'valued'), ...]
```

## 接进任何 agent：MCP / HTTP / CLI 三条路

不是某个平台的专用件。三个接入面，覆盖 2026 年主流 agent：

| 接入面 | 谁用 | 一行启动 |
|---|---|---|
| **MCP**（stdio，8 个工具） | Claude Code、Codex CLI、Cursor、Cline、Windsurf、Zed、Gemini CLI | `memory-palace-mcp` |
| **HTTP + OpenAPI** | Dify、Coze、n8n、自定义 GPT Actions、低代码 | `memory-palace-http --port 8765` |
| **CLI / 库** | 自己写循环的 agent、脚本、定时任务 | `memory-palace context --query "…"` |

Claude Code / Cursor 的 MCP 配置（同一份 JSON，Codex 用 TOML）：

```json
{ "mcpServers": { "memory-palace": { "command": "memory-palace-mcp",
  "env": { "MEMORY_PALACE_STORE": "~/.memory-palace/store.json" } } } }
```

```bash
memory-palace context --query "仓位怎么分配" --budget 800   # ← 会话开始：注入一块
memory-palace remember "用户要求所有输出表格化" --tags 偏好 --tier fact
memory-palace outcome m:719af3d5 contradicted               # 用户纠正了你
memory-palace cycle                                         # 会话结束：沉淀/遗忘/自述/反思
```

`context` 的输出可以直接拼进 system prompt，**钉死的硬规则永远在**（哪怕与当前问题无关），
并带可信度提示：

```
# 记忆上下文（候选 12 条 → 取 5 条，412/800 字，语义检索「仓位分配」）
- [硬规则] 禁止绝对化措辞，所有判断都带概率（钉死）
- [仓位] 半凯利：单笔不超过总资金三分之一
- [教训] 追高后逆板块阴跌，亏三个点，很后悔（细节可能失真 0.67）

## 自述（第 4 版）
起源「输出」2条 → 转折「复盘」3条 → 当下「仓位」2条｜可靠度 0.91｜单主题占比 0.32
```

为什么不给 `search(query) -> chunks`：agent 真正缺的不是相似文档，而是
**「我该记住什么、该放下什么、这条还能不能信」**。

- 各宿主的完整配置（Claude Code / Codex / Cursor / Cline / Windsurf / Zed / Gemini CLI /
  Dify / Coze / n8n / LangChain / OpenAI function calling）：[docs/INTEGRATIONS.md](docs/INTEGRATIONS.md)
- 三种接入时机（注入/固化/反馈）、多库划分、**什么时候不该用**：[docs/AGENT_INTEGRATION.md](docs/AGENT_INTEGRATION.md)
- agent 读得懂的约定：[AGENTS.md](AGENTS.md)

## 看得见的记忆（这是最容易被忽略的一层）

记忆系统改的是 prompt 里的一小段 —— **用户看不见**。「用了很久」和「根本没生效」在体验上完全一样。
所以这里有三层可见物：

**① 一行状态**（每次注入后，agent 用它交代「这次用了什么」）

```bash
memory-palace context --query "仓位怎么配" --status-only
[记忆 6/9 · 钉死 2 · 147/900字 · 自述 rev1]
```

**② 终端视图**（谁最重要、谁最快被忘掉）

```bash
memory-palace view
```
```
记忆宫殿 · 9 节点 / 3 边 / 墓碑 3 / 事件 12
价值 flat（top1 占 15%）｜结构 isolated（孤立 67%）

重要性              寿命      记忆
██████████████ 1.55 29天     [复盘] 追高汉缆后逆板块阴跌，亏三个点，很后悔
██████████···· 1.14 钉死      [硬规则] 禁止绝对化措辞，所有判断都带概率
██████████···· 1.14 74天     [仓位] 半凯利仓位：单笔不超过总资金三分之一
██████████···· 1.14 极稳（>5年）  [硬规则] 风险提示必须放在末尾，不构成投资建议

30 天内将跌破遗忘线：3 条（noise:1、lesson:win、loss:0914）
已遗忘：noise:2、old:1、tmp:1
```

**③ HTML 面板**（遗忘是*怎么*发生的）

```bash
memory-palace render --out palace.html    # 自包含单文件，双击即开，无 CDN/无图片
```

含四块：记忆卡片（重要度 + 剩余寿命 + 可靠度）、**未来 60 天衰减曲线**（钉死的是平线）、
注意力份额、墓碑（被主动放下的，仍可溯源）。
不装任何东西也能先看：[docs/sample-palace.html](docs/sample-palace.html)（用演示库生成）。

三条读法：条形长但寿命短 = 现在很亮但很快会忘（如果不重要，这是对的）；
曲线最先触底的就是系统认为最该放手的；墓碑不是失败，是取舍 —— 全部保留等于没有取舍。

## 十个机制，以及它们各自守住的不变量

| 模块 | 管什么 | 关键不变量 |
|---|---|---|
| `core` | 节点、共现图、**幂等衰减**、原子持久化 | 权重是「锚点 + 时间」的纯函数：GC 跑 1 次和 100 次，账本逐位一致 |
| `affect` | 潮水（唤醒）/ 河床（峰值）/ 失真代价 + **评价器** | 写情绪 ≠ 强化记忆；情绪越高越难忘，也越不可靠 |
| `logic` | 有向带类型的推理边 | 共现不是凭据，相似不是推出；矛盾沿推理链传播，只标记不裁决 |
| `consolidation` | 碎片 → 结论 | 单向门：结晶后碎片降级为 trace，只留溯源窗口 |
| `value` | 重要性四分量 / 熵 / **结构熵** / 代谢预算 | 结论按价值取舍，碎片按物理强度退场 —— 两套遗忘律 |
| `narrative` | 自述 = 投影 | 图里不会多出一个「我」；叙事地位会随时间退潮 |
| `persona` | 基座 / 人格 / 人设 / 修订四层 | 基座系统只读，改它需要人给理由；人设可换可回滚 |
| `memory_model` | 五种记忆五种寿命 | 工作记忆超容量是**降级**不是删除；身份记忆不参与遗忘 |
| `reflection` | 指标化回看 | 只提建议，绝不自动改配置 |
| `selfcorrect` | 自修正 | 白名单 + 值域 + 冷却 + 可回滚 + **拒绝用滞后指标判决** |

### 三条反直觉的设计决定

**1. 衰减必须是纯函数，不是余额。**
把「衰减」写成每天扣一点，跑两次 GC 就扣两次 —— 系统的遗忘速度会取决于你多久扫一次。这里权重恒等于 `base × exp(-Δ天 / 稳定性)`，任何时刻读都是同一个数。

**2. 情绪不是加成，是乘在稳定性上。**
如果情绪直接加权重，它只是「更亮」；加在稳定性上，它改变的是**衰减速度** —— 这正是「怎么抓都抓不住的东西偏偏最难忘」的机制。代价也照收：唤醒越高，编码越容易被改写，`fidelity` 随峰值唤醒下降，且不随时间恢复。

**3. 自我叙事是读法，不是条目。**
上一版把「自我」写成 `self:reflection` 节点，等于说「我」是一条会衰减、会被检索强化、会被 GC 扫走的事实。现在自述是每次重新投影出来的章节 + 选择理由（留下谁、放下谁、为什么），不落成节点。

## 证据：它凭什么说自己是对的

```bash
python -m memory_palace.demo        # 215 项断言，18 条突变审计
python tests/test_cli.py            # 21 项 CLI 契约（含展现层）
python tests/test_integrations.py   # 15 项 MCP / HTTP 协议层契约
```

自测不是「跑通就算过」，里面有三类专门用来打自己的脸的东西：

- **元测试（突变审计）**：故意把实现弄坏（衰减带副作用、去掉裁决器、假语义全零向量、白名单失效、结构熵写死……），断言**必须变红**。18 条里有一条自己写错过（用 `lr=1.0` 制造「无惯性」，结果一步撞天花板，饱和伪装成惯性），已改成常数学习率的反事实并写进注释。
- **故障注入**：序列化写一半断电、fsync 失败、replace 失败、replace 与清理同时失败 —— 旧快照必须完好、临时文件必须清干净。
- **精确计数**：8 线程 × 25 轮并发后，`access_count` 必须精确等于 `基准 + 200`；重复名单不得双重强化。

## v0.1.0 修掉的七个已知局限

| 局限 | 现在的做法 | 证据 |
|---|---|---|
| 叙事地位单调饱和 | 加 `narrative_heat` 泄漏积分 + `narrated_at`，地位随时间退潮 | 同样讲 5 次：最近讲的 central=1.0，半年前讲的 0.003 → GC 只删后者 |
| 概念词典手工、`资金` 跨主题误召回 | 多义词按上下文分配份额；概念词从语料学习（df + ≥2 锚点 + lift + 纯净度） | 板块语境 `资金` 93.5% 归板块；未登录词「梭哈」自学进 position |
| 情感二维无来源 | 评价器从文本词表 + 程度副词推断，运行时用结果反馈；带 `source`/`drivers` | 「亏…极其后悔」→ v=-0.46 / a=0.80 / 依据 `('亏','后悔','!极其')` |
| 熵只算重要性 | 增加结构读数：度熵 / 孤立占比 / 连通分量 / 碎片度 | 6 条互不相连 → isolated_share 1.0；全连通 → components 1.0 |
| 自修正只有 7 个参数、验证滞后一轮 | 12 个参数 + 逐参数值域 + 观察窗样本 + 样本过旧拒绝判决 | 实测 `semantic_min_score` 连回滚 2 次后被永久收权 |
| Windows 目录 fsync 静默降级 | `CreateFileW + FlushFileBuffers`，返回 durable 并记账 | 本机 `durable=True`；注入失败返回 False |
| 双失败留 1 个 tmp | 清理重试 + 下次写盘回收 ≥300s 的孤儿 | 手放陈旧 tmp，下次写入自动清 |

## 成熟度自评（先说不好听的）

| 维度 | 分数 | 依据 |
|---|---|---|
| 设计完整度 | 9/10 | 机制闭环，次序有理由，边界写在 docstring 里 |
| 工程实现 | 7/10 | 原子写、RLock、零依赖、状态版本迁移；但**没有跨进程文件锁** |
| 测试可信度 | 7/10 | 215 断言 + 18 突变；但断言是作者自证，无覆盖率门槛、无 property-based、无 fuzz |
| 可维护性 | 6/10 | `palace.py` 1357 行、`demo.py` 2167 行；剧本同时兼任测试 |
| 生产就绪 | 3/10 | 单写者假设、无性能基准、无语义评测集、API 未冻结 |

**四个最大的风险，写在这里而不是藏在 issue 里：**

1. **语义层不是 embedding。** 概念种子 + 字符 bigram + 语料学习，在小语料上可解释、可审计；语料一上万条会退化。它换来的是零依赖和「为什么命中」可追问，代价是召回上限。
2. **测试通过 = 我写的断言通过。** 18 条突变只覆盖我想到的破坏方式，样本偏差仍在。
3. **自修正的「变好」仍是单指标。** 加了噪声带和观察窗样本，但没有离线回放/反事实评估，长期看仍可能把波动当趋势。
4. **单写者。** 两个进程写同一快照会互相覆盖（写是原子的，锁不是跨进程的）。

**不适用**：把它当生产级记忆后端、当向量检索替代品、或存高价值不可再生数据。**适用**：研究记忆机制、给 agent 做可解释的长期记忆原型、当教材读。

## 项目结构

```
memory_palace/
  core.py          节点 / 图 / 幂等衰减 / 配置 / 原子持久化
  affect.py        情感三维 + 评价器（情绪从哪来）
  logic.py         有向推理图（凭据、传递、矛盾传播）
  consolidation.py 沉淀（双通道聚类 + 单向门）
  value.py         重要性 / 熵 / 结构熵 / 代谢预算 / 审计判决
  narrative.py     自述投影（章节、选择理由、可靠度、垄断度）
  persona.py       基座 / 人格 / 人设 / 修订
  memory_model.py  五种记忆五种寿命
  reflection.py    指标化回看（只提建议）
  selfcorrect.py   自修正（白名单 / 值域 / 冷却 / 回滚 / 抗滞后）
  semantics.py     语义检索（多义词裁决 + 词典自增长）
  palace.py        顶层编排（每轮固定次序）
  cli.py           命令行：remember / context / view / render / cycle / outcome / explain
  render.py        展现层：状态行 / 终端视图 / 自包含 HTML 面板（衰减曲线）
  mcp_server.py    MCP 服务（stdio，8 个工具）—— Claude Code / Codex / Cursor / Cline 等通用
  http_api.py      本地 HTTP + OpenAPI 3.0 —— Dify / Coze / n8n / GPT Actions 通用
  demo.py          20+1 幕剧本与自测（215 项断言 + 18 条突变审计）
examples/quickstart.py
tests/test_cli.py            CLI 契约（21 项：退出码 / 预算 / 无幽灵 / 空库不自修正 / 三层展现）
tests/test_integrations.py   MCP 与 HTTP 协议层契约（15 项：握手 / 工具 / 错误语义 / OpenAPI）
docs/ARCHITECTURE.md         设计细节与次序的理由
docs/INTEGRATIONS.md         各宿主配置：Claude Code / Codex / Cursor / Dify / Coze / n8n / LangChain…
docs/AGENT_INTEGRATION.md    三种接入时机、成本、以及不该用它的场景
AGENTS.md                    agent 读得懂的约定（Codex / Claude Code 自动加载）
```

## 版本策略

`0.x` 期间 API 可能变。判定 1.0 的条件写在 [CHANGELOG](CHANGELOG.md)：外部使用者 ≥ 1、CI 覆盖三个平台、语义层有独立评测集、跨进程写锁落地。

## License

MIT © 2026 Howard
