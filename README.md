# memory-palace

> 一个会遗忘、会被情绪加固、会讲自己故事的记忆系统。
> 零第三方依赖 · 三行接入任意 agent。

[![CI](https://github.com/Y5H1ggs/memory-palace/actions/workflows/ci.yml/badge.svg)](https://github.com/Y5H1ggs/memory-palace/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)
[![deps: 0](https://img.shields.io/badge/dependencies-0-brightgreen.svg)](pyproject.toml)

![记忆面板](docs/images/dashboard.png)

<p align="center"><sub>↑ 由 <code>memory-palace render</code> 从一份 120 天的演示库直接生成（<a href="docs/sample-palace.html">在线样例</a>）。图上的每个数字都能用一条命令复现。</sub></p>

**TL;DR (English)** — A dependency-free Python implementation of a *memory lifecycle*: idempotent decay, affect that both hardens and distorts recall, consolidation with a one-way door, value-based forgetting with a fixed attention budget, self-narrative as a projection, tree-shaped injection to save tokens, and self-correction bounded by whitelist + value ranges + anti-lag verification. Ships MCP / HTTP / CLI. 340 assertions, 18 mutation audits. Not a vector database; not production-ready.

---

## 它解决的不是「存不下」，是「记不住该记的」

| 问题 | 向量库 / RAG | 这里 |
|---|---|---|
| 三个月没想起的事，该不该还在？ | 在，除非你删 | 按强度 + 价值判，还会随叙事地位退潮 |
| 情绪强烈的事是不是更难忘？代价是什么？ | 不知道 | 是，而且**越情绪化越可能是错的** |
| 十条碎片什么时候变成一条结论？ | 不会 | 结构 + 语义双通道结晶，单向门 |
| 我「选择」忘掉什么，凭什么？ | 不涉及 | 四分量重要性 + 审计理由 + 固定预算 |
| 「我是谁」是图里的一个节点吗？ | 是 | 不是。自述是读图的方式，是投影 |
| 我上个月的主张，现在还算数吗？ | 两句话一起端给你，自己猜 | 旧事实**到期**而不是被删：默认只取现在的，`as_of` 能回看当时的 |
| 我问的东西它其实没记过，会怎样？ | 把最像的几条凑上来 | **弃答**：明确说「没有相关记忆」，只保留钉死的硬规则 |
| 同一句话记了三次呢？ | 三个近似条目互相挤预算 | 几乎一字不差就合并；补了新内容的不合并（长度比把关） |
| 记忆多了，注入会不会撑爆上下文？ | 会 | 树状注入：根常驻、枝按需展开、其余折叠 |
| 系统能不能自己调参？到哪停？ | 不涉及 | 12 个白名单参数，超范围只能上报给人 |

一句话：**这里建模的是记忆的生命周期，不是检索的一次调用。**

## 60 秒上手

```bash
git clone https://github.com/Y5H1ggs/memory-palace.git && cd memory-palace
python examples/quickstart.py     # 不装包也能跑
python -m memory_palace.demo      # 215 项断言 + 18 条突变审计（约 1 秒）
```

```python
from memory_palace import MemoryPalace, MutableClock

palace = MemoryPalace(clock=MutableClock())
palace.remember("rule:test", "说「完成」之前先跑一遍自测，绿了才算完成", tier="core", pinned=True)
palace.remember("ci:cp1252", "CI 在 Windows 上因为控制台编码炸了，排查两小时", tags=("复盘", "教训"))

palace.recall(query="怎么避免重复踩坑")        # 语义召回：标签零重叠也命中
palace.graph.get("ci:cp1252").affect         # 情绪由文本推断，带依据
palace.run_cycle()                           # 沉淀 → 抓取 → 遗忘 → 自述 → 人格 → 反思 → 自修正
```

命令行等价（agent 走这条）：

```bash
memory-palace context --query "怎么避免线上事故" --budget 800   # ← 会话开始：注入一块
memory-palace remember "周五不发布，出事没人修" --tags 流程,教训
memory-palace outcome m:719af3d5 contradicted                  # 用户纠正了你
memory-palace cycle                                            # 会话结束：代谢一轮
```

## 看得见：三层可见物

记忆系统改的是 prompt 里的一小段 —— **用户看不见**。「用了很久」和「根本没生效」，体验上一模一样。
所以这里有三层可见物，都来自同一份真实数据：

| | 命令 | 看到什么 |
|---|---|---|
| **一行状态** | `context --status-only` | `[记忆 6/9 · 钉死 2 · 147/900字 · 自述 rev1]` |
| **终端视图** | `view` | 谁最重要、谁最快被忘掉、已经忘了什么 |
| **HTML 面板** | `render --out x.html` | 单文件自包含：寿命分布、进出账、卡片、自述、墓碑 |

<img src="docs/images/terminal-view.png" width="700" alt="终端视图">

<img src="docs/images/terminal-tree.png" width="700" alt="树状注入">

面板不是仪表盘，是**记忆的体检报告**。三条读法：最先触底的那几条就是系统认为最该放手的；
条形长但寿命短 = 现在很亮、但很快会淡（多数琐事本该如此）；墓碑不是失败，是取舍 ——
全部保留，等于没有取舍。

> 演示库里显示「无梯度」是真实结论，不是 bug：17 条记忆的年龄与使用频次太接近，
> 系统诚实地告诉你「这套记忆还没有形成取舍」。

## 接进任何 agent：MCP / HTTP / CLI

不是某个平台的专用件。三个接入面，语义完全一致：**`context` 注入 / `remember` 固化 / `cycle` 代谢**。

| 接入面 | 谁用 | 一行启动 |
|---|---|---|
| **MCP**（stdio，8 个工具） | Claude Code、Codex CLI、Cursor、Cline、Windsurf、Zed、Gemini CLI、**DSH** | `memory-palace-mcp` |
| **HTTP + OpenAPI 3.0** | Dify、Coze、n8n、自定义 GPT Actions、低代码 | `memory-palace-http --port 8765` |
| **CLI / 库** | 自己写循环的 agent、脚本、定时任务 | `memory-palace context …` |

```json
// Claude Code / Cursor：.mcp.json
{ "mcpServers": { "memory-palace": { "command": "memory-palace-mcp",
  "env": { "MEMORY_PALACE_STORE": "~/.memory-palace/store.json" } } } }
```

```yaml
# DSH（DeepSeek Harness）：~/.dsh/profiles/<profile>/cordis.patch.yml
- insert:
    - id: mcp-memory-palace
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: memory-palace
        transport: stdio
        command: python
        args: ['-u', '-m', 'memory_palace.mcp_server', '--profile', 'slim']
        cwd: /path/to/memory-palace
        env:
          MEMORY_PALACE_STORE: /Users/you/.memory-palace/store.json
          PYTHONPATH: /path/to/memory-palace
        toolCallTimeoutMs: 30000
```

- 各宿主完整配置（Claude Code / Codex / Cursor / Cline / Windsurf / Zed / Gemini CLI / DSH /
  Dify / Coze / n8n / LangChain / OpenAI function calling）：[docs/INTEGRATIONS.md](docs/INTEGRATIONS.md)
- 三种接入时机、多库划分、**什么时候不该用**：[docs/AGENT_INTEGRATION.md](docs/AGENT_INTEGRATION.md)
- agent 读得懂的约定：[AGENTS.md](AGENTS.md)

### 省 token 的两个开关

DSH 这类宿主会把**全部工具定义常驻上下文**（不支持 Tool Search），每轮都要付 schema 的钱：

```bash
memory-palace-mcp --profile slim    # 只暴露 context / remember / cycle 三个工具
memory-palace-mcp --profile full    # 8 个（默认）
```

## 树状记忆：分层付费

扁平注入每条都要付全额 token。12 条还能忍，120 条只有两条路：截断（丢刚需）或全塞（爆预算）。
树状给第三条：**根常驻、命中的枝展开到叶子、其余折叠成一行摘要**，并显式标注「另有 N 条」。

```
# 记忆树（49 条 → 根 4 / 枝 6，展开 1 枝，412/700 字）
├─ [钉死] 说「完成」之前先跑一遍自测，绿了才算完成
├─ [钉死] 改数据结构必须同时升状态版本并写迁移…
├─ 复盘（14）· CI 在 Windows 上因为控制台编码炸了…
│  · 排查两小时，教训：终端输出统一 UTF-8
│  · …另有 12 条（recall 取）
└─ 其余 3 个主题（21 条）：工程、流程、琐事
```

`python -m memory_palace.bench`，1000 条记忆的实测：

| | 耗时 | 注入字数 |
|---|---|---|
| 扁平（6 条） | 23 ms | 371 字 |
| 扁平（30 条，要覆盖面） | 31 ms | 1184 字 |
| **树（同覆盖面）** | **8 ms** | **603 字（省 49%）** |

诚实说明：**窄预算（只要 6 条细节）时树反而略贵**（603 vs 371 字），因为它默认给全貌。
两种注入不是替代关系，是两个档位 —— 要细节用扁平，要知道「我有什么」用树。

## 跨项目记忆：分层，而不是二选一

![作用域](docs/images/scopes.png)

真实用法不是「全共享」或「全隔离」，而是**两层**：

| 层 | 放什么 | 换项目后 |
|---|---|---|
| `global` | 用户偏好、硬规则、通用教训 | **还在** |
| `proj:owner/repo` | 这个项目的架构决策、踩过的坑 | 不出现 |

```bash
memory-palace remember "以后输出用短段落" --scope global    # 跨项目共享
memory-palace remember "部署脚本要先 source env.sh"          # 默认落到当前项目
memory-palace context --query "部署" --scope project        # 只看本项目
memory-palace context --query "部署" --scope all            # 调试：全部作用域
memory-palace promote lesson:ci                             # 把项目教训升为通用
memory-palace projects                                      # 看分布
```

项目标识按 `--project` → `MEMORY_PALACE_PROJECT` → **git remote**（`proj:owner/repo`）→ 目录名+哈希
依次推断。用 git remote 而不是路径，是因为换目录、换机器、重新 clone 都该是同一个项目。

三个不显然但重要的决定：

1. **沉淀不跨项目**。A 项目的碎片和 B 项目的碎片绝不结晶成一条「结论」——
   那条结论会被两边都当成自己的。
2. **失效与作用域分开判**。被取代的旧事实由「历史保留期」回收，不参与价值 GC，
   否则一条曾经很重要的旧偏好会靠高重要性永远赖着。
3. **面板与读数跟随视图**。看着项目视图却报全库的熵，会让人误判「我的注意力很集中」，
   其实是被别的项目的数据摊平了。

## 设计

![架构](docs/images/architecture.png)

三条不变量，其余都是推论：

1. **权重是「锚点 + 时间」的纯函数**，不是余额。GC 跑一次和一百次，期末逐位相同
   （旧实现每天扣一点，遗忘速度取决于你多久扫一次）。
2. **情绪乘在稳定性上，不加在权重上**。加权重只是「更亮」；乘稳定性改变的是**衰减速度** ——
   这才是「怎么抓都抓不住的东西偏偏最难忘」。代价照收：唤醒越高，编码越可能被改写。
3. **叙事是投影，不是节点**。图里不会多出一个「我」；自述每次重新计算章节与选择理由。

| 模块 | 管什么 | 关键不变量 |
|---|---|---|
| `core` | 节点 / 图 / 幂等衰减 / 双时态 / 原子持久化 | 状态版本 v6，兼容读 v4/v5；事实有有效期 |
| `affect` | 潮水 / 河床 / 失真代价 + 评价器 | 写情绪 ≠ 强化记忆 |
| `logic` | 有向带类型的推理边 | 共现不是凭据，相似不是推出 |
| `consolidation` | 碎片 → 结论 | 单向门；碎片降级为溯源窗口 |
| `value` | 重要性 / 熵 / 结构熵 / 预算 | 结论按价值、碎片按强度，两套遗忘律 |
| `narrative` | 自述投影 | 叙事地位随时间退潮 |
| `persona` | 基座 / 人格 / 人设 / 修订 | 基座系统只读 |
| `memory_model` | 五种记忆五种寿命 | 超容量是降级不是删除 |
| `reflection` | 指标化回看 | 只提建议，绝不自动改配置 |
| `selfcorrect` | 自修正 | 白名单 + 值域 + 回滚 + 拒绝滞后指标 |
| `semantics` | 语义检索 | 多义词按上下文裁决；词典从语料自学 |
| `tree` | 树状注入 | 折叠必须显式标注「另有 N 条」 |
| `cli` | agent 入口（注入 / 写入 / 代谢） | 无关查询必须弃答；钉死规则永远在 |

## 证据

```bash
python -m memory_palace.demo        # 215 项断言 + 18 条突变审计（剧本 21 幕）
python tests/test_cli.py            # 30 项 CLI 契约（含树状注入 / 作用域 / 宿主配置 / 三层展现）
python tests/test_integrations.py   # 17 项 MCP / HTTP 协议层契约（含 slim 档）
python tests/test_temporal.py       # 23 项 双时态 / 去重 / 弃答（含反事实）
python tests/test_scope.py          # 24 项 跨项目分层 / 隔离 / 提升 / 迁移
python tests/test_extensions.py     # 14 项 编码器 / 外部反思的契约
python tests/test_lexicon.py        # 16 项 词法层（含 IDF 反事实）
python -m memory_palace.bench       # 性能与 token 基准
```

自测里最有价值的不是「全绿」，是那 18 条**突变审计**：故意把实现弄坏（衰减带副作用、去掉风格裁决、
假语义全零向量、白名单失效、结构熵写死……），断言必须变红。其中一条自己写错过 ——
用 `lr=1.0` 制造「无惯性」，结果一步撞天花板，饱和伪装成惯性；已改成常数学习率的反事实并写进注释。

## 词法层：语义的地基，也是最容易悄悄骗人的地方

完整设计与性能账见 [docs/LEXICON.md](docs/LEXICON.md)。

原来的字符维是「每对字符等权加一，哈希进 64 个桶」——于是 `输出`（满语料都是）
和 `凯利`（只出现两次）权重相同。后果不是"召回不准"，是**沉淀把无关的两件事写成一条结论**。

现在：

| 处理 | 做法 | 一个具体后果 |
|---|---|---|
| 归一 | NFKC + 小写 | `ＵＴＦ－８` / `utf-8` / `UTF8` 是同一个词 |
| 分词 | 中文双字 + 标识符（`env.sh` → `env.sh`/`env`/`sh`） | 技术文本里的标识符不再是噪声 |
| 否定词 | `不没无非别勿禁未莫` 单字保留并 ×2 | 「不追高」与「追高」余弦 0.359，不是 1.0 |
| IDF | `log((N+1)/(df+1))+1`，语料现算 | 见下 |

**核心证据**（`tests/test_lexicon.py` 的反事实）：

| | 「构建机输出乱码」vs「输出用表格」 | 会聚吗 |
|---|---|---|
| 关掉 IDF | 0.783 | 会（错） |
| 打开 IDF | **0.448** | 不会（对） |

同时「输出用表格」与「输出优先精简」仍然聚成一类——**该分的分开，该合的还合**。

代价如实记账：200 条 `cycle` 19→51 ms，热查询 1.5→3.1 ms（维度 64→256）。
三个缓存把损失从 12 倍压到 2.6 倍。详见 [docs/LEXICON.md](docs/LEXICON.md)。

## 两个扩展点：默认零依赖，但你可以换掉

默认实现是明码标价的取舍，不是天花板。两个钩子都在构造函数上，不传就是当前行为。

**① 语义编码器** —— 默认是「概念种子 + 字符 bigram」，抓不到同义改写：

```python
class BgeEncoder:                      # 任意 embedding：本地 ONNX / API 都行
    dim = 512
    def encode(self, text): return model.encode(text, normalize_embeddings=True).tolist()

palace = MemoryPalace(encoder=BgeEncoder())   # 沉淀与检索**同时**换掉
```

实测：同义改写 cosine 0.057 → 0.949；标签不同也能聚成一类。
`python examples/custom_encoder.py` 可直接看对比输出。

**② 外部反思** —— 默认是规则化监控（阈值 → finding），不是 LLM 反思：

```python
palace = MemoryPalace(reflector=lambda payload: ask_llm(json.dumps(payload)))
```

三条护栏：只追加观察并标来源、**不能改配置**（参数仍归规则与自修正）、
失败只记 `reflector_error` 不炸穿代谢。

为什么不是默认开：加上模型就不是零依赖了。为什么保留：规则监控只能发现"预设的问题"，
发现不了"我没预料到的模式"—— 后者才是反思。

三条常见批评的逐条核查（含一条被截断的代码引用）见
[docs/CRITIQUE_RESPONSE.md](docs/CRITIQUE_RESPONSE.md)。

## 与同类项目的取舍

Mem0 / Zep(Graphiti) / Letta / Cognee / TencentDB Agent Memory / progressive-memory 都看过一遍，
结论写在 [docs/COMPETITIVE_ANALYSIS.md](docs/COMPETITIVE_ANALYSIS.md)。摘要：

- **融合了三处**（都是补语义漏洞，零新依赖）：Zep 式**双时态**、LongMemEval 单列一档的**弃答**、
  Mem0 式**写入去重**（外加一条它们也没写的长度比约束）。
- **拒绝了五项**：LLM 抽取事实、上图数据库、换真实 embedding、core memory 全量常驻、多租户面板。
  理由分别是：破坏零依赖 / 只换来"看起来专业" / 放弃离线可审计 / 预算会被吃光 / 那是服务端产品的事。
- **还缺一件**：**组件级评测集**。业界那三个 benchmark 已经出现"评测腐化"——LoCoMo 全文仅约 26k tokens，
  128k 上下文的模型直接塞进去就能得高分；独立审计发现它约 6.4% 的答案键是错的。
  所以下一步不是加功能，是建一个 50 条真实记忆 + 20 条查询、能测**遗忘正确性**与**弃答假阳性率**的评测。

## 成熟度自评（先说不好听的）

| 维度 | 分数 | 依据 |
|---|---|---|
| 设计完整度 | 9/10 | 机制闭环，次序有理由，边界写在 docstring 里 |
| 工程实现 | 7/10 | 原子写、RLock、零依赖、状态迁移、基准；但**没有跨进程文件锁** |
| 测试可信度 | 7/10 | 340 断言 + 18 突变 + 3 条反事实；但断言是作者自证，无覆盖率门槛、无语义评测集 |
| 可维护性 | 6/10 | `palace.py` 1300+ 行、`demo.py` 2100+ 行；剧本兼任测试 |
| 生产就绪 | 3/10 | 单写者、无性能 SLA、无语义评测集、API 未冻结 |

**五个最大风险**：① 语义层不是 embedding（概念种子 + 字符 bigram + 语料学习），语料上万条会退化；
⑤ **没有组件级评测集** —— 弃答线与去重阈值都是基于实测底噪选的，换个语料就会漂；
② 测试通过 = 我写的断言通过；③ 自修正的「变好」仍是单指标；④ 单写者（写是原子的，锁不跨进程）。

**不适用**：生产级记忆后端、向量检索替代品、存不可再生数据、多进程并发写同一份记忆。
**适用**：研究记忆机制、给 agent 做可解释的长期记忆原型、当教材读。

## 项目结构

```
memory_palace/
  core.py              节点 / 图 / 幂等衰减 / 配置 / 原子持久化
  affect.py            情感三维 + 评价器（情绪从哪来）
  logic.py             有向推理图
  consolidation.py     沉淀（双通道聚类 + 单向门）
  value.py             重要性 / 熵 / 结构熵 / 预算 / 审计判决
  narrative.py         自述投影
  persona.py           基座 / 人格 / 人设 / 修订
  memory_model.py      五种记忆五种寿命
  reflection.py        指标化回看
  selfcorrect.py       自修正
  semantics.py         语义检索（多义词裁决 + 词典自增长）
  tree.py              树状注入（根常驻 / 枝展开 / 折叠标注）
  scope.py             作用域：全局 / 项目 / 会话，项目标识推断
  lexicon.py           词法层：归一 / 分词 / 否定词 / IDF
  palace.py            顶层编排
  cli.py               命令行
  mcp_server.py        MCP 服务（stdio，8 工具，slim/full 档）
  http_api.py          本地 HTTP + OpenAPI 3.0
  render.py            展现层（状态行 / 终端视图 / HTML 面板）
  bench.py             性能与 token 基准
examples/quickstart.py   60 行示例
examples/showcase.py     生成 120 天演示库与面板
examples/make_images.py  重新生成 README 配图（截图脚本）
examples/custom_encoder.py 换掉默认编码器（同义改写 → 可聚类）
docs/ARCHITECTURE.md     设计细节与次序的理由
docs/INTEGRATIONS.md     各宿主接入配置（含 DSH）
docs/AGENT_INTEGRATION.md 三种时机、成本、不该用的场景
docs/COMPETITIVE_ANALYSIS.md 同类项目调研：融合了什么、拒绝了什么、还缺什么
docs/CRITIQUE_RESPONSE.md 三条批评的逐条核查：哪条成立、哪条论据被截断
docs/LEXICON.md          词法层：四个决定、性能账、边界
dsh/                     DeepSeek Harness 插件（host 半 + cordis 挂载配置 + 面板）
AGENTS.md                agent 读得懂的约定
tests/test_temporal.py   双时态 / 去重 / 弃答（含反事实断言）
tests/test_scope.py      跨项目分层 / 隔离 / 提升 / 旧快照迁移
tests/test_extensions.py 编码器 / 外部反思两个扩展点的契约
tests/test_lexicon.py    词法层：归一 / 分词 / 否定 / IDF（含反事实）
```

## 版本

`0.x` 期间 API 可能变。升到 `1.0.0` 的四条硬条件写在 [CHANGELOG](CHANGELOG.md)：
外部使用者 ≥ 1、CI 覆盖三平台、语义层有公开评测集与基线数字、跨进程写锁落地。

## License

MIT © 2026 Howard
