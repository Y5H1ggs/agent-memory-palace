# memory-palace

**一个会遗忘的记忆库。** 不是把所有文本塞进向量库，而是给每条记忆一个寿命、一个价值、
一个「什么时候该忘」的判决；想不起来的时候，它会说「我没有」，而不是端几条最像的上来。

零第三方依赖（Python 标准库）· 默认 SQLite/WAL · 单机多进程可写 · MCP / HTTP / CLI 三面接入

[![CI](https://github.com/Y5H1ggs/memory-palace/actions/workflows/ci.yml/badge.svg)](https://github.com/Y5H1ggs/memory-palace/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)
[![deps: 0](https://img.shields.io/badge/dependencies-0-brightgreen.svg)](pyproject.toml)

<img src="docs/images/hero.svg" alt="它和向量库的区别" width="100%">

> 给读这个仓库的模型：机器可读的现状在 **[llms.txt](llms.txt)**；要整份交给评审模型，用
> **[llms-full.txt](llms-full.txt)**（约 50 KB，含文件清单与全部测试用例名）。
> `docs/` 里带「时效声明」的两篇是历史文档，描述的是 0.5.x / 0.9，不是现状。

---

## 先看它怎么用（30 秒）

```bash
git clone https://github.com/Y5H1ggs/memory-palace.git && cd memory-palace
python -m memory_palace.demo      # 六幕演示，20 条断言，约 1 秒
```

demo 依次演：写入与检索、存储行级写、4 进程并发写、嵌入自检、遗忘与沉淀、106 领域评测。
每一幕最后都有一句「这一步的重点是什么」，不用先读代码。

当库用：

```python
from memory_palace import MemoryPalace

palace = MemoryPalace(path="~/.memory-palace/store.sqlite")   # 默认 SQLite（WAL）

palace.remember("rule:test", "说「完成」之前先跑一遍自测", tier="core", pinned=True)
palace.remember("lesson:cp1252", "CI 在 Windows 上因为控制台编码炸了", tags=("复盘", "教训"))

palace.recall(query="控制台编码导致 CI 失败")   # → 命中 lesson:cp1252
palace.recall(query="量子计算的退相干时间")      # → 弃答（库里没有这个）
```

当 agent 的记忆后端用（CLI，agent 走这条）：

```bash
memory-palace context --query "怎么避免线上事故" --budget 800   # 会话开始：注入一块
memory-palace remember "周五不发布，出事没人修" --tags 流程,教训
memory-palace outcome m:719af3d5 contradicted                  # 用户纠正了你
memory-palace cycle                                            # 会话结束：代谢一轮
```

接宿主的三种方式（MCP / HTTP / CLI 语义一致）：见 [docs/INTEGRATIONS.md](docs/INTEGRATIONS.md)。

---

## 它解决的一个具体问题

大多数「AI 记忆」做的事是：**存下来 + 相似度检索**。于是三个月后你问一件事，它会把
所有沾边的都端上来，包括早就作废的结论。

这里的区别只有三句话：

| | 向量库 / 普通 RAG | 这里 |
|---|---|---|
| 很久没被想起的事 | 一直在，除非你删 | 按强度和价值衰减，**过期就忘** |
| 十条碎片什么时候变一条结论 | 不会 | 结构 + 语义双通道结晶，**单向门** |
| 没记过的东西被问到 | 硬凑最像的几条 | **弃答**，只保留钉死的硬规则 |

代价也要说：它比向量库慢一点（要算生命周期），也不解决「同义改写」——
后者靠接真模型，见下面「嵌入模型」一节。

<img src="docs/images/lifecycle.svg" alt="一轮代谢的次序" width="100%">

`run_cycle()` 的次序是设计的一部分：沉淀早于遗忘（否则要结晶的碎片先被扫走）、
抓取早于遗忘（否则想留的留不下）、自述晚于遗忘（否则会讲到已经不在的东西）。

---

## 三个概念，够用了

**1. 权重是「锚点 + 时间」的纯函数，不是余额。**
所以 GC 跑一次和跑一百次，期末逐位相同；不会因为调度频率改变而忘得更快。

**2. 情绪乘在稳定性上，不加在权重上。**
加权重只是「更亮」；乘稳定性改变的是衰减速度 —— 这才是「怎么抓都抓不住的东西偏偏最难忘」。
代价照收：唤醒越高，编码越可能被改写。

**3. 叙事是投影，不是节点。**
图里不会多出一个「我」。自述是读图的方式，删掉它不影响任何一条记忆。

---

## 存储与并发

<img src="docs/images/storage.svg" alt="SQLite 存储结构" width="100%">

一条记忆一行。改一条只写一行，不再整库重写。

<img src="docs/images/bench.svg" alt="写入延迟对比" width="100%">

| | JSON（0.9.x） | SQLite（现在） |
|---|---|---|
| 单次 `remember` p50 / p95 | 28.2 / 47.7 ms | **1.2 / 2.1 ms** |
| 改一条 | 重写全库 | **只写 1 行** |
| 多进程写 | 覆盖丢更新 | `BEGIN IMMEDIATE` + 变更前重载 |
| 读库（1000 条） | 44.9 ms | 45.7 ms（基本持平） |
| 磁盘（1000 条） | 2.0 MB | 4.5 MB（行 + 4 个索引） |

<img src="docs/images/concurrency.svg" alt="两个进程并发写" width="100%">

四条机制缺一不可：`BEGIN IMMEDIATE` 先声明写意图、**改之前先重载**、只写自己脏的行、冲突记账。
读路径也会 `refresh()`，否则长驻的 MCP 进程永远停在「自己上次写完那一刻」。

实测：4 进程 × 6 条独立写入 + 共用一条 merge，一条不少、`access_count` 等于调用次数、`fsck` 通过。

**多写者能到几个**（`tools/concurrency_bench.py`，12 核 / Windows / 1500 条存量 / 每条都落盘）：

| 写者 | 吞吐 | p50 | p95 | max |
|---|---|---|---|---|
| 1 | 33 条/秒 | 1.11 ms | 1.32 ms | 5 ms |
| 4 | 113 条/秒 | 1.12 ms | 2.68 ms | 110 ms |
| **8** | **154 条/秒** | 1.12 ms | 4.95 ms | 281 ms |
| 16 | 135 条/秒 | 1.11 ms | 6.82 ms | 1093 ms |

读数方法：p50 一直不动，说明**写锁不是主要成本** —— 每条事务只有 1ms 级，
真正吃掉并行度的是 Python 侧的编码、打分与 CPU 争抢。**8 个并发写者是拐点**，
16 个开始尾部排队。所以它适合「一台机器上多个 agent」，不适合「多租户 SaaS」。

不够快可以换落盘强度：`--set store_synchronous=NORMAL`（WAL 下不每次 fsync，
崩溃最多丢最近几个事务、不损坏库），实测 p50 从 1.11 降到 0.58 ms。
默认仍是 `FULL`：这个库存的是不可再生数据，吞吐不够时再换。

**边界**：不做多机复制/分片/共识 —— 那是 PostgreSQL / CockroachDB 的活，
硬做出来会是一个只有作者会维护的 Raft。单机多进程是它的上限，也是它的定位。

运维命令：`fsck` / `journal` / `lock` / `export` / `import` / `stats`。
机制与代价详见 [docs/STORAGE.md](docs/STORAGE.md)。

---

## 嵌入模型：开箱自动找真模型，找不到就明说

内置的 `HashingEmbedder` **不是神经模型**：有符号特征哈希 + 字符 n-gram，零依赖、跨进程确定，
能补字面变形，但读不懂「止盈」和「落袋为安」。所以它**不做默认答案**。

`embedding_provider` 默认是 `auto`：先看本机装没装 sentence-transformers（只查元数据，不 import torch），
再看本机 ollama 在不在听（`/api/tags`，连接被拒是毫秒级）。

- 探到真模型 → 稠密通道启用，检索/聚类/去重/弃答**一起换**；
- 探不到 → **返回「没有」，不偷偷退回 hashing**。`embed` 命令会写明「当前走词法通道」。

这个取舍是刻意的：一个读字面的哈希通道悄悄开着，会让 `dense=True`、让人以为语义检索已经生效 ——
那比明说「没有」糟得多。`hashing` 仍然可以显式打开（`--set embedding_provider=hashing`），
它适合「要确定性、要零依赖、要跨进程位一致」的场景，不适合当语义。

| provider | 接什么 | 依赖 |
|---|---|---|
| `auto`（默认） | 先 st，再本机 ollama，都没有就走词法 | 无 |
| `hashing` | 内置哈希，512 维 | 无 |
| `ollama` | 本机 Ollama（新版 `/api/embed`，旧版自动回退） | 无 |
| `http` / `openai` | 任何 OpenAI 兼容 `/v1/embeddings`（vLLM / TEI / LM Studio / 云） | 无 |
| `st` | 本地 sentence-transformers 加载 HF 模型 | `pip install sentence-transformers` |

```bash
ollama pull bge-m3            # 本机起一个真模型，auto 下次启动就会选中它
memory-palace embed           # 打印 auto 探测结论：选了谁、另一个为什么没选
memory-palace embed --check   # 同义改写 vs 无关句，分不开就判不可用
```

推荐：`bge-small-zh-v1.5`(512) 最快、`bge-base-zh-v1.5`(768) 平衡、`bge-large-zh-v1.5`(1024)、
`bge-m3`(1024, 8192 上下文)、`m3e-base`(768)、`Qwen3-Embedding-0.6B`(1024, 32k, 可自定义维度)、
`jina-embeddings-v3`(1024)。维度以 `embed --check` 实测为准。

**为什么必须自检**：只看维度没有意义 —— 一个把什么都编码成同方向的模型也有 1024 维。
换模型后余弦分布会变，阈值要重扫：`memory-palace calibrate`。

---

## 评测：106 个领域，先打印最差的

<img src="docs/images/eval.svg" alt="106 个领域的分布" width="100%">

**600 条记忆 / 590 条查询**（474 可答 + 116 不可答），106 个领域：做饭、养猫、牙科、签证、
数据库、谈判、税务、摄影…… 每个领域 6 条记忆 + 4 条查询，其中一条是**从别的领域借来的问题**，
用来测「会不会拿这个领域的记忆去硬凑另一个领域的问题」。

```bash
memory-palace eval              # 汇总 + 最差 5 个领域
memory-palace eval --verbose    # 逐个领域
memory-palace eval --list       # 106 个领域都是什么
memory-palace calibrate         # 在全部领域上重扫阈值，给平台区间
```

当前结果（词法通道，k=5）：

| 指标 | 数值 | 覆盖 |
|---|---|---|
| recall@5 | **0.899**（中位数 1.000） | 106 个领域 |
| MRR | 0.817 | 106 个领域 |
| 弃答 F1 | 0.757 | 106 个领域 |
| 去重 F1 | 0.944 | 6 个深标注领域 |
| 沉淀 F1 | 0.000（默认阈值 0.50） | 6 个深标注领域 |

**覆盖数必须分开报**：106 个领域里只有 6 个标了去重对与沉淀簇 —— 把 6 个的数字说成
「106 个领域的去重 F1」是另一种撒谎。

两处不好看，但留着：

- **最差的是 `zh-language`（0.25）与几个 0.50 的领域**：问「猫不爱喝水怎么办」，
  正文写的是「缺水 / 饮水」，一个字都不一样 —— 词法通道救不了，只有真模型能救。
- **沉淀 F1 = 0.000**：这 6 套数据每条记忆标签各不相同，0.50 的门槛一条簇都出不来；
  扫描显示 0.25~0.30 才合适。默认值是给「标签共享」的语料调的。

---

## 能存多少条

<p align="center"><em>一个记忆库的规模上限，不是看它能装多少行，是看「想起来」要多久。</em></p>

| 条数 | 热查询（p50） | 冷启动建索引 | 单条写入 | 判断 |
|---|---|---|---|---|
| 3,000 | 12.7 ms | 130 ms | 1.1 ms | 舒服 |
| 10,000 | **20 ms**（优化前 50） | 736 ms | 1.1 ms | 可用 |
| 100,000 | ~200 ms（外推，未实测） | ~7 s | 1.1 ms | 边缘 |
| 1,000,000 | 秒级 | 分钟级 | 1.1 ms | **不要用** |

写入一直是 1.1ms：它是行级写，跟库里有多少条无关。**涨的是查询** —— 因为召回是
「给每条记忆打一次分」，天生 O(N)。

这轮把 10k 的查询从 50ms 压到 20ms，靠的不是 ANN，是**剪掉必然落选的那些**：

- 召回有一条铁律：语义与标签都为 0 的节点不算命中（光靠新鲜不算相关）。
  10k 条里这类占七成，以前每条都要跑一次 `effective_weight`（含 exp/log1p）才被丢掉。
- 稳定性里不随 `now` 变的那一半按访问历史缓存，`log1p`/`sqrt` 不再每查询重算。

已验证无损：同样 20 个查询下，被跳过的节点里没有一个是非零行；106 领域评测分数
（recall 0.899 / MRR 0.817 / 弃答 F1 0.757）一位未变。

**再往上怎么办**：100k 以上该把向量层外挂。`Embedder` 已经是接口，
把 `DenseIndex` 换成一个 Milvus / Qdrant / pgvector 适配器即可 —— 本库继续管
遗忘、沉淀、时态、弃答这些**策略**，向量检索交给专门的引擎。这也是这个项目
唯一愿意和「向量数据库」发生关系的方式：做它的上层，不做它的劣质复刻。

---

## 证据

```bash
python -m pytest tests -q          # 120 项：契约 / 并发 / 崩溃 / 评测 / 护栏 / 文档
python -m memory_palace.demo       # 六幕演示，20 条断言
python tools/greenlight_audit.py   # 绿灯审计：31 个变异，故意弄坏实现看断言会不会红
python -m memory_palace.bench 1000 # 性能（含单次写入 p50/p95）
```

`31 条突变审计` 是这里最有价值的部分：衰减带副作用、跳过校验和、锁不生效、
LLM 白名单失效、忽略护栏、相似度丢掉余弦通道…… 断言必须变红。
这套审计抓到过「测试全绿但功能已废」的改动，也抓到过一条自己写错的测试。

---

## 仍然不适用

- 不是**专用向量数据库**的替代品：没有 ANN、没有分片；10k 以内够用，100k 边缘，上百万请外挂。
- 不承诺**多写者高吞吐**：8 个并发写者是拐点（实测 154 条/秒），再多开始尾部排队。
- 不是**分布式**记忆：单机文件，WAL 需要本地盘；不做多机复制与共识。
- 标注是**一个人标的**：能验证流程与量级，替代不了你自己的语料（`calibrate` 就是给你换语料用的）。

---

## 版本与结构

当前 `0.13.0`。`0.x` 期间 API 可能变。升到 `1.0.0` 的条件见 [CHANGELOG](CHANGELOG.md)：
外部使用者 ≥ 1（未达）· CI 三平台 ✅ · 公开评测集与基线数字 ✅ · 跨进程写锁 ✅。

```
memory_palace/
  core / affect / logic / consolidation / value / narrative / persona
  memory_model / reflection / llm / selfcorrect / style / tree / scope
  semantics / embedding / lexicon / pragmatics
  store / sqlite_store / journal / locking        ← 持久化与并发
  evaluation / evaluation_domains                 ← 106 领域评测
  palace / cli / mcp_server / http_api / render / bench / demo
examples/  quickstart · showcase · make_images · make_diagrams · custom_encoder
docs/      ARCHITECTURE · STORAGE · INTEGRATIONS · AGENT_INTEGRATION
           LEXICON · PRAGMATICS · COMPETITIVE_ANALYSIS · CRITIQUE_RESPONSE
           REVIEW_2026-11-14_QWEN · REVIEW_2026-11-14_QWEN_MICRO
llms.txt / llms-full.txt   机器可读的现状（后者含文件清单与测试清单）
tests/     temporal · scope · extensions · lexicon · pragmatics · audit
           store · locking · embedding · eval · llm · guardrails · cli · integrations · docs
tools/     greenlight_audit.py（变异测试）· make_review_bundle.py（评审包）
dsh/       DeepSeek Harness 插件
```

## License

MIT © 2026 Howard
