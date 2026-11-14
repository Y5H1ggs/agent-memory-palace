# Changelog

所有值得写下来的变更都在这里。格式参照 [Keep a Changelog](https://keepachangelog.com/)，版本号遵循 [SemVer](https://semver.org/lang/zh-CN/)。

## 为什么第一个公开版本是 0.1.0 而不是 1.0.0

SemVer 里 `1.0.0` 的含义是「公开 API 已稳定，可以放心依赖」。这个项目现在还不满足：

- 没有任何外部使用者，API 是按内部需要长的（`run_cycle` 的返回结构、`MemoryPalace` 的构造参数都可能变）；
- 语义层是概念种子 + 字符 bigram + 语料学习，没有独立评测集，召回上限未知；
- 只有单写者假设，没有跨进程文件锁。

**升到 1.0.0 的条件**（四条全部满足）：外部使用者 ≥ 1、CI 覆盖 Linux/Windows/macOS、语义层有公开评测集与基线数字、跨进程写锁落地。

---

## [0.13.0] - 2026-11-14

针对外部评审的三条批评做的一轮：**开箱体验**、**反思闭环**、**并发与规模边界**。
三条里两条是真问题，一条（LLM 白名单）是设计意图被误读 —— 但误读的那条也指出了
一个真实缺陷：拒绝之后没有反馈，模型永远不知道发生了什么。

### 嵌入：默认 `auto`，但绝不假装有语义

- `embedding_provider` 默认值从 `""` 改为 **`auto`**：先查本机是否装了
  sentence-transformers（`find_spec`，不 import torch），再探本机 ollama 的
  `/api/tags`（连接被拒是毫秒级返回，不拖启动）。
- **关键取舍：`auto` 探不到真模型时返回 `None`，不退回 `hashing`。**
  评审说「默认是玩具级」—— 根子不是 hashing 存在（它零依赖、确定性，对未登录词有平滑），
  而是它被当成了默认答案。悄悄开一个读字面不读意思的通道，会让 `dense=True`、
  让用户以为语义检索已经生效 —— 比明说「现在走词法」糟得多。
- 构造失败要降级：装了 st 但没下过模型、又断网，是最常见的现场。用户点名了模型
  → 失败就是失败；没点名 → 允许退回 ollama。
- `embed --check` 增加 `advice`：判决之外给一句能照着做的话。

### 反思：把「被拒」告诉模型

- `LLMReflector(max_rounds=2)`：第一轮出现白名单外/方向非法的提案时，
  带着拒绝原因再问一轮，要求它只从清单里重选。**只在真的越界时才付第二次调用。**
- 白名单外的东西不再只剩一个参数名：`insights` 保留原话与理由，标成「需人工裁决」，
  `reflect` 命令会打印出来。白名单是「不许它生效」，不是「不许它说话」。
- `ALLOWED_PARAMS` 改为由 `PARAM_BOUNDS` 派生（单一真相源）。分开写时存在
  「白名单有、值域没有」的洞，`PARAM_BOUNDS.get(p, (1e-4,1))` 会用通用区间悄悄放行。
- 第二轮失败不推翻第一轮 —— 有结论比「全绿但没有」好。

### 并发：实测拐点，而不是形容词

- 新增 `tools/concurrency_bench.py`：N 进程 × M 次逐条落盘的吞吐/p50/p95 曲线。
  本机（12 核 / Windows / WAL / FULL）实测：
  `1 写者 33 条/秒 → 4 写者 113 → 8 写者 154（拐点）→ 16 写者 135`。
  **写锁不是主要成本，p50 恒定 1.1ms**；16 写者时的尾延迟（~1s）来自 CPU 争抢与排队叠加。
- 写入的瞬态重连从 1 次提到 3 次（带退避）。16 进程并发写时实测出现过
  `attempt to write a readonly database`，一次重连不够 —— 新连接仍可能落在
  别人切换 WAL 的窗口里。修完后 320/320 全成功（原 300/320）。
- 新增配置 `store_synchronous`（`FULL`/`NORMAL`，默认 `FULL`）。
  WAL + NORMAL 下每次 COMMIT 不 fsync，崩溃最多丢最近几个事务、不损坏库；
  实测 NORMAL 把 p50 从 1.11ms 压到 0.58ms，吞吐基本不变。**默认仍给 FULL** ——
  这个库存的是不可再生数据，吞吐不够时再换，换法写进了 README。

### 检索：10k 条时省掉七成无用功

- `_score_components(only_hits=True)`：召回本来就有「语义或标签必须非零」的门槛，
  所以两者都为 0 的节点**不可能**被返回，它们唯一作用是让 recency 项算出来给人看。
  10k 条里这类占七成，每条都要跑一次 `effective_weight`（含 exp/log1p）。
  recall 现在跳过它们；`score_breakdown` 仍返回全量解释。已验证等价：
  20 个查询下，被跳过的节点里没有一个是非零行。
- `MemoryNode._static_stability()`：稳定性里不随 now 变的那一半（层级 × 类型 ×
  访问历史）按 `(tier, kind, access_count, last_accessed, created_at)` 缓存。
  缓存写入走 `object.__setattr__`，绕开脏标记 —— 一次查询把全库标脏，
  下一次保存就会全量重写，那比慢更糟。
- 实测 10k 条：热查询 **50ms → 20ms**（2.5×），写入 p50 不变。
- 优化后 100k 条查询约 200ms 量级，仍不适合百万级 —— 见 README 的「规模边界」一节。

### 文档

- README 增加「这个库适合多大」：给出 3k/10k/100k 的实测数字与「什么时候该换外部向量库」，
  不再只用一句「适合个人助理」含糊过去。

---

## [0.12.0] - 2026-11-14

三件事：**评测从 6 个领域扩到 106 个**、**demo 重写**、**README 重写**。
扩领域的过程本身抓出了两个真 bug，也逼着改掉了三处「说得比做得多」的表述。

### 评测：6 → 106 个领域

- 新增 `memory_palace/evaluation_domains.py`：**100 个广覆盖领域**，每个 6 条记忆 + 4 条查询，
  共 600 条记忆 / 400 条查询。领域覆盖做饭、养猫、牙科、签证、数据库、谈判、税务、摄影、
  剪辑、排版、徒步、露营、天文、语言学习等。
- 每个领域额外注入一条**跨领域的不可答查询**（从别的领域借一条真实提问）：
  它测的是「会不会拿 A 领域的记忆硬凑 B 领域的问题」，比手写「今天天气怎么样」有信息量得多。
  合计 **590 条查询**（474 可答 + 116 不可答）。
- `DATASETS` 改为**惰性构建**：CLI 启动不该为 100 个数据集付 160ms（首次访问只花 8ms）。
- 报告改版：默认只印汇总 + **最差 5 个领域**，`--verbose` 逐个看，`--list` 列出全部领域。
  覆盖数单独报（检索 106 / 弃答 106 / 去重与沉淀 6）—— 把 6 个领域的去重 F1 说成
  「106 个领域的去重 F1」是另一种撒谎。
- `calibrate` 修正：去重曲线只在**标了去重对**的领域上算，不再拿「没标注」的领域投 0 分。
- 当前：recall@5 **0.899**（中位数 1.000）、MRR **0.817**、弃答 F1 **0.757**、去重 F1 **0.944**。

### 扩领域抓出的两个真 bug

1. **`recall(names=[...], as_of=...)` 不筛有效期**：名字入口绕过了时态过滤，被取代的旧事实
   会和新事实一起返回，而 docstring 写着「默认只返回现在有效的」。已修（`is_valid_at(moment)`）。
2. **`consolidate()` 的 `created` 是名字列表**，demo 里当计数用，`list > 0` 直接抛 TypeError。
   这不是库的错，但说明「返回值形状」在文档里没写清 —— demo 改成 `len(created)`。

### demo 重写（`python -m memory_palace.demo`，约 1 秒）

六幕，每幕一句「这一步的重点」：

1. 写入与检索：标签命中、弃答、以及**同义改写会弃答**（词法通道的真实短板）
2. 存储：改一条只写一行；**截断的库必须被拒绝**（改中间字节可能落在空闲页，测不出来）
3. 多进程：4 进程 × 6 条，一条不少、`access_count` 等于调用次数
4. 嵌入：`self_check` 现场跑一遍，并给出换真模型的三行命令
5. 生命周期：GC 幂等、沉淀、`as_of` 回看、过期回收（钉死的不动）
6. 评测：106 领域汇总 + 最差 3 个

写 demo 时踩到一个坑并写进注释：`MutableClock.advance()` 的单位是**天**，
`advance(30)` 会把 `tier=event` 的记忆直接衰减到 0，后面的沉淀就无从谈起。

### README 重写

「看得似懂非懂」是结构问题，不是措辞问题。这版按「它是什么 → 30 秒怎么用 →
它解决的一个具体问题 → 三个概念 → 存储与并发 → 嵌入 → 评测 → 证据 → 边界」排，
每节不超过一屏；把「106 个领域」的**覆盖边界**（去重/沉淀只有 6 个）写在数字旁边。

---

## [0.11.2] - 2026-11-14

外部评审的第二份（微优化建议，5 条）。逐条核对：**2 条采纳、1 条更彻底地采纳、1 条早已实现、1 条不采纳**。
逐条证据与数字：[docs/REVIEW_2026-11-14_QWEN_MICRO.md](docs/REVIEW_2026-11-14_QWEN_MICRO.md)。
结果：117 个 pytest、绿灯审计 **31/31**。

### 采纳

- **沉淀聚类的常数开销**：`_pairs()` 改生成器（600 候选 = 17.9 万对，原来先建列表再迭代一次，
  白占约 2 MB）；`_clusters()` 预取 `frozenset(node.tags)`（同一节点的标签集合会被重建上百次）。
  实测 600 条候选 **36.8 ms → 32.9 ms**。新增 `_similarity_sets()` 与原版逐对对拍
  （>500 组，误差 < 1e-12），防两份实现漂。
- **反思统计合并为一遍遍历**：原来建窗口 1 遍 + `count()` 6 遍 + payload 求和 3 遍；
  现在一遍循环同时过滤、计数、累加。5000 条事件单次 `run()` **1.94 ms → 1.16 ms**。
- **配置回滚覆盖所有异常**：`apply_suggestion` 原来只认 `ValueError`，若 `validate()`
  漏出 `TypeError`，内存里会留下「没校验过、也没落盘」的参数，重启静默变回去。
  现在 `except Exception` 回滚后原样抛出（不吞），并补测试。

### 不采纳（附理由）

- **聚类的「无共享标签就跳过」**：本项目相似度是 `max(structural, 0.85*cosine)`，不是 Jaccard。
  照它改会把「换了说法」的语义聚类整条路径关掉，**而且测试不会红** —— 这正是最危险的改法。
  替代方案是上面那两条常数优化，收益相同、行为不变。
- **反思窗口用 `bisect`**：`events` 不保证单调（`restore()` 读回旧事件、测试用 `MutableClock`、
  机器时钟可能回拨）。对非单调序列二分不报错，只给错误切片 —— 反思窗口悄悄少一段，指标看着还合理。
  在一个上限 5000 条、单次 1.9 ms 的场景里，不值得为它维护有序索引。

### 一个副作用：审计差点漏两个

采纳第 1 条后，绿灯审计从 30/30 掉到 **29/31**。两个原因都已修：

- 为省一次 set 而复制了相似度算术 → 变异只改到一份。现在 `_similarity` 委托给
  `_similarity_sets`，只有一份实现。
- 早停测试的反事实 patch 的是 `_similarity`，而 `_clusters` 已改调 `_similarity_sets`，
  补丁没生效、两次跑同一段代码。现在 patch 正确对象，并断言「向量查表次数更少」，
  证明早停真的被走过。

### 早已实现

- **`dataclass_field_names` 缓存**：0.9.3 就是 `lru_cache` + `frozenset`，理由写在 docstring 里
  （1000 条记忆要问 2.5 万次 `dataclasses.fields()`）。

---

## [0.11.1] - 2026-11-14

一次「评审为什么看不到东西」的收尾。

### 现象

外部评审说「仓库里没有 SQLite」「palace.py 1300 行、只有 store.json、0.5.x」。
核对结果是**它读的不是仓库，是一段粘贴文本**，而且那段文本停在 0.10.0 之前：

| 事实 | 评审的说法 | 仓库实际 |
|---|---|---|
| SQLite 后端 | 「没看到」 | `sqlite_store.py` **898 行**，0.10.0 引入 |
| palace.py 规模 | 「1300+ 行」 | **2054 行** |
| 默认存储 | 「store.json」 | `~/.memory-palace/store.sqlite`（0.10 起） |
| 测试 | 「缺少并发测试」 | `tests/test_store.py` 492 行，4 进程丢更新测试每次 CI 都跑 |

模型看不了硬盘，也常拿不到 GitHub 实时内容 —— 它读的就是你给它的那段文本。
只要那段文本旧了或截断了，它会非常自信地得出「这里没有 X」，而且**内部逻辑自洽**，
看不出破绽。这不是模型的错，是我们没给它一份能自证完整的材料。

### 做法

- 新增 `tools/make_review_bundle.py`：生成 **`llms-full.txt`**（约 50 KB / 32K 字符），内容为
  `llms.txt` + `README.md` + 文件清单（路径 / 行数 / sha256 前 8 位 / 模块首行说明）+
  全部测试用例名 + 最新 CHANGELOG。
  不塞完整源码（几十万行，模型反而看不进去），但**把存在性与规模摆明白**：
  「有没有 sqlite_store.py」在这份文件里是一行表格，不是需要猜的事。
  生成器的体积报**字节**而不是字符 —— 中文一个字符三字节，按字符说 KB 会少报一半。
- `--check` 模式：文件过期就返回非零。接进 `tests/test_docs.py` 与 CI ——
  **代码改了、包没重生成，构建就红**。
- 评审的正确用法写进 README 与 llms.txt：把 `llms-full.txt` 整份粘贴，
  或直接给 `https://raw.githubusercontent.com/Y5H1ggs/memory-palace/main/llms-full.txt`。

---

## [0.11.0] - 2026-11-14

三件事：**可换真神经模型**、**评测集从 2 个领域扩到 6 个**、**把「文档比代码旧」这个坑堵上**。
后两件都是被外部评审逼出来的 —— 它把一个 0.5.x 的判断当成现状，追查下去发现仓库里
确实还留着那些话。

### 嵌入：接入真的神经语言模型

- 新增 `OllamaEmbedder`（零依赖）：新版 `/api/embed` 批量、旧版 `/api/embeddings` 单条自动回退，
  回退后不再试探新端点。
- 新增 `SentenceTransformerEmbedder`（`provider=st`）：本地加载 HF 模型，懒 import，
  没装就报一句能照做的话。
- `provider` 从 `""/hashing/http` 扩到 `""/hashing/http/openai/ollama/st`。
- 新增 `self_check()` 与 `memory-palace embed --check`：用「同义改写 vs 无关句」的余弦差
  判断模型能不能用。**只看维度没有意义** —— 一个把什么都编码成同方向的模型也有 1024 维。
- `llms.txt` 里列了 9 个推荐模型与维度，并写明换模型后必须重扫阈值。

### 评测：2 个领域 → 6 个领域

新增 `zh-medical`（用药与就医）、`zh-legal`（合同与合规）、`zh-support`（客服工单）、
`zh-personal`（个人助理）。合计 **135 条记忆、90 条查询**（74 条可答 + 16 条不可答）。
`run_suite` 增加跨领域汇总（每领域指标的算术平均，不按条数加权）。

扩领域立刻抓出三个真问题：

1. **标签没进索引**：正文写「不吃香菜与内脏」、标签写「忌口」，
   查询「我有什么忌口」得分是 **0.000** —— 不是排序靠后，是根本没进候选集。
   新增 `index_tags`（默认开），六个领域平均 recall 0.889 → **0.908**。
2. **哈希桶碰撞制造假分数**：`CHAR_DIM=256` 时，「量子计算的退相干时间是多少」
   对一条讲破产概率的金融记忆打出 **0.455**，两者一个共享词项都没有。
   桶数提到 1024 后同一查询只剩真正的「时间」匹配（0.219）。稀疏向量不因维度变大而变重。
3. **弃答默认值在两领域上过拟合**：0.20 在六领域上误弃 4 条、比 0.15 一条也没多抓到。
   默认值改为 **0.14**（平台 0.04~0.14 的上界；90 条查询上误弃 0、漏弃 1）。
   这已是这个默认值第二次被标注集推翻（0.30 → 0.20 → 0.15）。

当前六领域平均：recall@5 **0.9204**、MRR **0.8149**、弃答 F1 **0.9762**、去重 F1 **0.9444**。

### 文档：把「比代码旧」变成会失败的测试

外部评审（Qwen）把 0.5.x 的判断当成现状，追查后发现仓库里确实还留着：

- `docs/INTEGRATIONS.md` 六处把默认存储写成 `~/.memory-palace/store.json`；
- `docs/COMPETITIVE_ANALYSIS.md` 的能力表写着「本库（0.5.x）」；
- `docs/CRITIQUE_RESPONSE.md` 大段引用 0.5/0.9 时代的自评（3/10、单写者、palace.py 1300 行）；
- `cli.py` 的模块 docstring 也写着 store.json。

处理方式不是删，而是**标注 + 机器可读的现状**：

- 新增根目录 **`llms.txt`**：当前版本、能力表、嵌入模型清单、六领域与数字、明确边界，
  并直接告诉读仓库的模型「docs 里那两篇是历史文档，别当现状」。
- 两份历史文档开头加时效声明，指向 `llms.txt`。
- 修正 `INTEGRATIONS.md` 的六处路径与 `cli.py` 的 docstring。
- 新增 `tests/test_docs.py`：版本号三处必须一致、README/llms.txt 必须写当前版本、
  任何 `.md` 不许再把 store.json 当默认、历史文档必须有时效声明。
  **文档再漂就会红**。

### 验证

113 个 pytest（100 → 113）· demo 全剧本 · 绿灯审计 **30/30** 变异被抓到
（新增两条：忽略标签索引、Ollama 只走旧端点）。

---

## [0.10.2] - 2026-11-14

README 重做（图文与内容对齐），外加一个 CI 上真实出现的并发缺陷。

### 文档

- **README 重写**：从「一屏代码 + 大段散文」改成「一张示意图 + 一段判断」，六张图由
  `examples/make_diagrams.py` 生成（纯 SVG，浅色卡片、GitHub 深色主题下也可读，
  文字已转义，不依赖媒体查询）：
  `hero`（是什么 / 不是什么）、`lifecycle`（代谢次序与每步判据）、`storage`（七张表与提交顺序）、
  `concurrency`（两个进程的时序）、`bench`（延迟对比）、`eval`（两条通道的召回）。
- 图上每个数字都来自 `bench` / `eval`，且写进 `NUMBERS` 常量，改数字重跑一条命令即可。
- 删掉与实现不符的段落：`0.9.x` 的断言数（215）、旧的性能数字、已不成立的「不支持多进程」。

### 数字复测（三次中位数，本机 Windows / Python 3.13）

| 指标 | 旧值（文档里写的） | 复测值 |
|---|---|---|
| JSON 单次 `remember` p50 | 47.1 ms | **28.2 ms** |
| JSON 单次 `remember` p95 | 53.2 ms | **47.7 ms** |
| SQLite 单次 `remember` p50 / p95 | 1.2 / 1.5 ms | **1.2 / 2.1 ms** |
| 读库（1000 条） | 43 → 49 ms | **44.9 → 45.7 ms（基本持平）** |
| 磁盘（1000 条） | 1.3 → 4.3 MB | **2.0 → 4.5 MB** |

写延迟比从「39×」更正为 **24×**。数据本身没变差，是之前那次测量用的是更长的文本；
现在把「测量条件」也写进 README，避免下次又对不上。

### 修复

- **写锁争用时重试，而不是让 `remember` 直接失败**（CI 在 2 核 runner 上 4 进程并发时
  偶发 `LockBusy`）。`_retry_locked()` 只在事务**未能开启**时重试（最多 3 次，50/100 ms + 抖动）；
  已进入事务后的失败不重试 —— 那会把做了一半的修改再做一遍。公开 `save()` 同享。
  新增 `test_write_retries_when_lock_is_held_briefly`（另一连接占锁 250 ms、`lock_timeout=50ms`）。

---

## [0.10.1] - 2026-11-14

外部评审核对后的一轮修复。评审（Qwen）的五条指控里，两条戳中真问题、一条部分成立、
两条引用的是 0.9.x 的旧代码。逐条核对写在 [docs/REVIEW_2026-11-14_QWEN.md](docs/REVIEW_2026-11-14_QWEN.md)。

### 修复

- **`apply_suggestion()` 落盘失败不回滚**（评审第 2 条，属实）：手动采纳参数时丢掉了
  `_maybe_save()` 的返回值 —— 磁盘满/权限问题下内存用新阈值跑、重启静默回滚、无日志。
  现在失败即回滚并抛 `StoreError`，并补上 `@_mutating`（手动改参数也走跨进程写事务）。
  新增 `test_apply_suggestion_rolls_back_when_save_fails` + 变异 `config-save-ignored`。
- **沉淀配对预算按桶顺序装满就停**（核对第 1 条时发现的隐患）：预算 6 万在 8 个标签桶里
  只够装满前半个桶，后面的主题**一对都不比较**且不报错。改成跨桶轮流取，覆盖率与主题顺序无关。
- **Windows + 多进程首次建库的 read-only 瞬态**（压测并发写时发现，失败率 4/20）：
  WAL 切换与建表相撞时 pager 进入 `SQLITE_READONLY_RECOVERY`，同一条连接上重试无效。
  改为建连阶段整体重连，并在写入路径上加一次瞬态重试；25 轮压测 0 失败。

### 补测试

- `test_gc_and_recall_race_on_ephemeral_nodes`：一边 gc 删、一边 recall 命中同一批
  **会被真删**的节点（评审第 4 条，属实 —— 之前那一幕只挑 pinned/core，gc 根本不删）。
  三组节点覆盖「必删 / 临界 / 新鲜」，断言不崩、不腐、不产生幽灵，并证明删除真的发生了。

### 澄清（不改代码）

- 聚类不是裸 `O(N²)`：候选封顶 600、配对封顶 6 万；实测 1000 条 80 ms、5000 条 87 ms（基本持平）。
- 反思已有 LLM 通道（可选），且参数震荡有冷却 / 每轮上限 / 永久拉黑 / 多目标护栏四道闸。
- 默认后端已不是 JSON：1 万条不再整份序列化，多进程写也不再是 Last-Write-Wins。

---

## [0.10.0] - 2026-11-14

把「不适用」的四个场景逐条变成可验证的能力：**生产级记忆后端 / 向量检索替代品 /
存不可再生数据 / 多进程并发写同一份记忆**，外加把「规则监控」升级成「LLM 反思」。

这一版的重点不是加功能，而是**把「我觉得」换成「测出来」**：
每个新能力都配了一条能失败的测试或一份带标注的数据。

### 存储：默认换成 SQLite（WAL 模式）

- 新增 `sqlite_store.py`：`nodes` / `edges` / `tombstones` / `blobs` / `events` /
  `commits` / `snapshots` 七张表，四个索引；`sqlite3` 是标准库，零依赖没破。
- **行级增量写**：节点带 `_dirty` 标记（`__setattr__` 置位、读库后清零），提交时只 upsert 变动的行。
  1000 条时单次 `remember` **p50 28.2 ms → 1.2 ms**（p95 47.7 → 2.1 ms），改一条 1.2 ms（三次中位数）。
- **提交账本 `commits`**：每次提交一行（revision / 时间 / 原因 / 写者 / 写入行数 / 摘要），
  只增不改；`events` 表只追加（内存里会 trim，库里不会）。
- **快照**：每 `snapshot_every`（默认 50）次提交留一份完整快照，只保留最近 3 份，
  `state_at(rev)` 用它对账。
- `store.py` 变成门面：`open_store()` 按扩展名/参数选后端，两个后端遵守同一份 8 方法契约。
- JSON 后端保留（`--backend json`）：单文件可移植、可进 git、可当冷备。

### 并发：多进程写同一份记忆

- `locking.py`：跨进程字节锁（POSIX `flock` / Windows `msvcrt.locking` / 哨兵文件兜底），
  可重入、总超时、持有者信息写旁路文件（Windows 字节锁下写锁文件本身会失败）。
- `palace._txn()`：所有写操作（25 个入口，`@_mutating` 标注）走「`BEGIN IMMEDIATE`
  → 若磁盘更新先重载 → 修改 → 提交」；重载发生在**修改之前**，这是不丢更新的关键。
- 冲突记账：`SaveResult.rebased` / `store.stats["conflicts"]`。
- 修掉三个真实缺陷：
  - 同名 `remember` 静默丢弃新内容（`add_node` 遇到同名原样返回）→ 现在就地更新并记 `replace`；
  - `--replace` 实际什么都没做，却打印「已记住」→ 现在先 `drop` 再写；
  - `save(other_path)` 会把当前库的写入路径切到副本 → 改成「复制一份」，不切换。
- 新增 `tests/test_store.py`：4 进程 × 15 次 merge 同一条记忆，`access_count` 恰好 60；
  `tests/test_locking.py`：真子进程争用、重入、总超时。

### 耐久：不可再生数据

- 快照带 `sha256`（v7 起；老快照按版本门控跳过校验，否则历史迁移会被误判成损坏）。
- JSON 后端额外带追加日志（CRC32 + zlib + 内容 sha256），快照丢失/损坏时前滚，
  断电写了半条则丢弃尾条 —— `journal.py`。
- 新增运维命令：`fsck`（完整性/悬空边/账本一致性/锁持有者）、`fsck --repair`、
  `journal`（提交账本）、`lock`（谁占着）、`export` / `import`（带 sha256 的可搬档案）、
  `stats`。
- `remove_store_files()`：删库要连 journal/rev/lock 一起删。只删快照会留下日志，
  下次打开会被「正确地」前滚回上一次的全部内容 —— demo 里踩过。

### 向量通道：真的 embedding，可换模型

- 新增 `embedding.py`：`Embedder` 协议 + `HashingEmbedder`（有符号特征哈希 +
  字符 n-gram + 子线性 TF，512 维，跨进程确定性）+ `HttpEmbedder`
  （任何 OpenAI 兼容 `/v1/embeddings`，批量 + 磁盘缓存 + 超时）+ `DenseIndex`。
- `SemanticIndex` 支持混合打分（`hybrid_weight`）、`lexical_scores()` /
  `dense_scores()` 分通道对照、`pair_score()`（两段任意文本，融合方式与检索一致）。
- 嵌入服务失败降级到词法通道并记 `dense_failures`，不会让检索整体不可用。

### 评测：组件级、带标注、两个领域

- 新增 `evaluation.py`：A 股复盘 / 工程运维两套数据（80 条记忆、42 条查询），
  四类标注（检索 gold / 弃答 / 去重对 / 沉淀簇），指标 recall@k、MRR、nDCG、P@1、
  弃答 F1、去重 F1、沉淀成对 F1。
- `memory-palace eval [--channel lexical|hybrid|dense] [--scale N...]`、
  `memory-palace calibrate [--apply]`（扫阈值 + 平台区间，不只给尖峰）。
- 实测（k=5）：混合通道把换说法召回从 **0.591 → 0.727**（finance），exact 上无增益；
  弃答默认阈值 **0.30 → 0.20**（F1 0.5 → 0.857，0.18~0.21 是平台区）；
  去重 F1 0.667（长度比保护挡掉了「百分之五 vs 5%」，保守是刻意的）；
  沉淀默认阈值 0.5 在「标签不共享」的语料上一条簇都出不来，扫描显示 0.25~0.30 才合适。
  **不利数字原样保留在 README 里** —— 不藏坏数才叫评测。

### 自修正：从单指标到多目标

- `Patch` 增加 `guards`（应用那一刻的护栏基线）与 `guard_hits`（判决时的越界幅度）。
- 判决要求：主指标改善 ≥ `selfcorrect_effect_min`，**且**没有护栏指标相对恶化超过
  `selfcorrect_guard_margin`（默认 15%）；越界即回滚并留下证据。
- `GUARD_DIRECTIONS` 显式声明每个指标的方向（越低越好 / 越高越好）——
  方向弄反会得到「越改越差才留下」的系统，有专门测试守着。
- `status()["guardrail_reverts"]` 可查哪些修正被护栏拦下。

### LLM 反思：模型提假设，代码管边界

- 新增 `llm.py`：`LLMProvider` 协议 + `HttpChatProvider`（OpenAI 兼容
  `/v1/chat/completions`）+ `LLMReflector`（证据拼装、预算截断、JSON 解析、白名单校验）。
- 反思证据从「只有条数」升级为「指标 + 事件 + 记忆样本（含内容）+ 自述健康度」——
  只给数字的模型只能复述数字。预算截断先丢样本、再丢事件，**指标与参数清单永不截断**
  （踩过：总文本直接截断把参数清单砍了一半，模型照着半份清单提建议）。
- 输出非法 JSON → 退回规则引擎并记 `degraded` + 原因；白名单外的参数一律丢弃并记
  `rejected_params`；**模型永远不改配置**，只产出待验证提案（走 `SelfCorrector` 的护栏）。
- 新命令 `memory-palace reflect`；`config --set` 支持改库内参数并写回快照。

### 其他

- `PalaceConfig` 新增 `backend` / `concurrency` / `journal_enabled` / `journal_keep` /
  `snapshot_every` / `lock_timeout` / `verify_checksum` / `embedding_*` / `hybrid_weight` /
  `llm_*` / `selfcorrect_guard_margin`，共 20 项；快照版本 **v6 → v7**（v4~v6 可直接读）。
- 默认存储位置 `~/.memory-palace/store.sqlite`；首次运行自动把旧的 `store.json`
  导入 SQLite，原文件保留为 `store.json.migrated`。
- CLI 新增 `--backend`，以及 `fsck` / `journal` / `lock` / `export` / `import` /
  `embed` / `eval` / `calibrate` / `reflect` / `stats` 十个子命令。
- 修掉 `tests/test_integrations.py` 被 pytest 误收集的三个 helper（`store` 参数被当成 fixture）。
- 新增 6 个测试文件（store / locking / embedding / eval / llm / guardrails），
  测试总数 43 → 97。

---

## [0.9.3] - 2026-11-14

第二轮：响应速度（端到端）与一份技术批评的核查。0.9.2 的进程内优化在上面，这里补的是
**用户真正感受到的那段时间** —— 起进程、读库、然后才是查询。

### 性能：端到端（1000 条记忆，本机中位数）

| 环节 | 前 | 后 | |
|---|---|---|---|
| 读库（JSON → 图） | 142 ms | **34 ms** | **4.2×** |
| `status`（起进程 + 读库） | — | 211 ms | 其中我们占 98 ms |
| `context --query` 端到端 | — | 269 ms | 其中我们占 156 ms |

三个罪魁，都是「每次调用都重复付的钱」：

1. **`dataclasses.fields()` 被当常量用**：反序列化一条记忆要问 20 多次「这个类有哪些字段」，
   1000 条 = 2.5 万次反射。字段集合在类定义时就定了，加 `lru_cache` 后读库砍掉一半。
2. **`import statistics` 17 ms**：只为求一个中位数，标准库这个模块导入就要 17 ms。
   换成五行本地 `_median`。
3. **`import html` 11.7 ms**：`render._esc` 只要合法化 5 个字符，却拖进 `html.entities`
   那张 2000 多条实体的表。换成 `str.translate`；同时把 `render` 在 CLI 里改成按需导入
   —— `context` / `recall` / `remember` 根本不需要它。
4. **索引改为按需构建**：读库时只置脏，不再立刻编码全库（1000 条约 37 ms）。
   `status` / `view` 这类不查语义的命令直接省掉；查询路径入口本来就有 `sync()`，语义不变。

### Fixed（第二轮审计 + 批评核查）

- **落盘失败时配置留在内存里（内存与磁盘永久撕裂）**：自修正改了配置、`validate()` 通过、
  但 `_maybe_save()` 失败 —— 磁盘还是旧值，重启静默回滚，而这一轮已经按新值跑完了，
  **没有任何人收到报错**。现在落盘失败会把本轮应用的修正全部退回去（`rollback_unpersisted`），
  并把 `last_save_ok` 暴露到 `snapshot()["durability"]`。
- **两个活视图遍历（同一个病的两处）**：`to_dict()` 遍历节点表、`edges()` 遍历邻接表，
  都直接读内部字典。`snapshot()` 持锁所以照不到，但这俩是公开方法，无锁调用时另一边
  `remove_nodes_from` 会抛 `RuntimeError: dictionary changed size during iteration`。
  两处都已物化。**第二处是 CI 抓到的**（ubuntu py3.10 的慢调度），本地跑 10 轮都没撞上 ——
  于是测试里把 GIL 切换间隔从 5ms 压到 1µs，让交错变成确定性的，变异测试才抓得住。
- **`_load_unlocked` 读库即建索引**（见上），改为懒建。

### Added

- **`tests/test_audit.py` 扩到 29 项**，新增：
  - 并发精确计数：5 线程 × 40 次读，`access_count` 必须**恰好** +200（零丢失，不是「大概对」）；
  - 并发边写边读边删：9 个线程同时 `remember` / `recall` / `gc` / `run_cycle`，零异常且钉死记忆不丢；
  - 无锁图序列化：边增删边 `to_dict()` 300 次不得抛异常；
  - 落盘失败回滚：**含反事实** —— 正常落盘时不许回滚（否则「永远回滚」也能让断言通过）。
- **10 个新变异**（共 22 个）：落盘失败不回滚、`to_dict` 活视图遍历、桶碰撞、永远全量重建、
  坏内容重试、早停返回错值、历史回收绕过钉死、预算被忽略、配置不校验、邻接表活视图。**22/22 全部被抓。**

### 关于「并发测试是幸存者偏差」这条批评

结论：**测试缺口成立，故障模式不成立**。原来的第 7 幕只让线程读已存在的 pinned / core 节点，
确实没测「边建边拆」。但批评预言的 `RuntimeError: dictionary changed size during iteration`
不会发生 —— `all_nodes()` 返回的是 `list(self._nodes.values())`（物化副本），
且公共入口都持 `RLock`。实测 5 线程抢同一条记忆计数恰好 201/201，9 线程混合压测 0 异常。
真正的漏洞在 `to_dict()`：它是唯一直接遍历内部字典的地方，已修并补测试（就是批评说的那个异常）。

### 关于「O(N²) 聚类在 5000 候选时卡死」

候选数被封顶在 `consolidate_max_candidates=600`、配对数封顶 `consolidate_max_pairs=60000`，
所以上界是「60000 次相似度」，不是「候选数的平方」。而 0.9.2 的早停让这其中**绝大多数**
根本不算余弦（结构通道已过线）。5000 条候选在这里不是可达状态。

自测：215 + 30 + 17 + 24 + 24 + 14 + 16 + 26 + 29 = **395**，22/22 变异被抓。

---

## [0.9.2] - 2026-11-14

一次代码审计 + 速度优化。结论先说：**性能问题的根不在算法复杂度，在四处「明明可以不做的活」**。

### 性能（1000 条记忆，本机，三次中位数）

| 指标 | 前 | 后 | |
|---|---|---|---|
| `cycle`（一轮代谢） | 530 ms | **108 ms** | 4.9× |
| 查询（冷，含全量重建） | 113 ms | **37 ms** | 3.0× |
| 查询（热） | 14.7 ms | **4.2 ms** | 3.5× |
| 200 条 `cycle` | 45 ms | **17 ms** | 2.7× |

### Changed

1. **稀疏向量（`SparseVector`）**。余弦原本是纯 Python 稠密循环：263 维里 245 个是零，
   一次 run_cycle 里 44k 次调用 = 1170 万次乘加。改成只遍历非零分量（平均 18 个），
   **12.2µs → 1.6µs**。`cosine()` 仍接受稠密列表（自定义 encoder 的契约不变），
   内部热路径（检索 / 聚类 / 去重）走稀疏分支。
2. **聚类早停**。`_similarity` 取 `max(结构, 0.85×余弦)`，而结构侧一旦过线，余弦再高也改变不了
   布尔结论 —— 但原来无条件照算。实测 1000 条语料 45262 对**全部**属于这种，等于每一对都在白算。
   现在结构过线即返回（对「是否过线」这个问题，答案完全相同，不是近似）。
3. **增量补建（`_fill_missing`）**。`sync()` 原先把「向量数 ≠ 节点数」当成「必须全量重建」，
   而沉淀每造一条结论节点，节点数就 +1 —— 一次 `run_cycle` 里 8 个簇就是 **8 次全量重建**
   （200 条记忆 = 1636 次编码）。改成只补缺失 O(k)，批末统一对齐一次。
4. **校验合并**。`_validate` 逐元素跑 `isinstance`+`isfinite`+`float` 四遍 = 27µs，
   与整个编码（30µs）相当。改成 `math.fsum` 一次 C 层扫描：非数值（TypeError）、
   NaN（`total != total`）、Inf（inf 或 ValueError）一并检出，**2µs**。
5. **编码直出稀疏**。`encode_with` 不再先铺 263 维数组再归一化；`project()` 不再顺带计算
   「中文 bigram 个数」—— 那个值三个调用方全部丢弃，却要每条记忆构造一个 24 元素集合。
6. 词项哈希桶（`_bucket_of`）与 `cjk_bigrams` 加 LRU：按**词**重复的活不再重算。

### Fixed（审计发现，按严重度）

- **配置不在入口校验**：`MemoryPalace(config=...)` 原样接受未验证的配置。后果不是报错而是**安静地做错事** ——
  `semantic_alpha=-5` 让检索永远返回空，`forget_w=-1` 让所有记忆立刻可忘，`grasp_budget=nan` 让预算判断全假。
  现在在构造函数里 `validate()`，那里还没读过任何状态，是唯一能干净失败的位置。
- **历史回收绕过 `pinned`**：`gc_decisions` 给钉死记忆的是 `pinned_by_user`（永不判掉），
  但 `_gc_unlocked` 的失效回收不看这一条 —— 一条被取代的钉死记忆会在保留期后自己消失。
  两条规则冲突时让更强的那个（用户显式钉死）赢。
- **`budget<=0` 仍然返回内容**：「至少给一条」的兜底覆盖了调用方的明确意图，等于让预算参数说了不算。
- **坏内容每次 `sync` 都重试**：编码失败的节点没有被记住，每次同步都重编一次必然会失败的向量 ——
  一条坏数据就能把「补缺失 O(k)」退化成「扫全库 O(n)」。现在记进 `_failed`，`rebuild` 时清空。
- **`project()` 每次白算集合**（见上），**`_graph_signature` 已成死代码**（脏标记取代后无人调用）。
- **`IdfModel` 类 docstring 与实现矛盾**：写着「未登录词拿到最高权重」，实现（和上一版的修正）是 0。
  文档说反了是隐性债务 —— 后来者会照文档去「修」正确的代码。
- 清理 9 处死导入（`bench` / `cli` / `memory_model` / `persona` / `pragmatics` / `style` / `demo` / `palace`）。

### Added

- **`tests/test_audit.py`（19 项）**：把审计结论变成断言。最关键的是一条**保序证明** ——
  同一批候选，早停前后的簇结构必须逐字相同（否则「优化」就是偷改语义）。
  另有：稀疏/稠密逐条对分（偏差 0.0e+00）、单位向量、坏配置在入口被拒、钉死不被回收、
  增量补建只编 1 条（按对象身份验证）、坏内容只尝试一次、20 次热查询 0 次重建 + 写入后恰好 1 次。
- **7 个新变异进绿灯审计**（共 19 个）：桶碰撞、永远全量重建、坏内容重试、早停返回错值、
  历史回收绕过钉死、预算被忽略、配置不校验。**19/19 全部被抓，0 漏洞。**

### 没做的事（诚实记账）

- **增量 IDF 没做**。`learn_concepts` + `IdfModel` 仍占冷查询约一半（1000 条 29ms），
  它们是对全语料的两遍扫描。增量版本要维护 doc 级 df 与删除回滚，做错的代价
  （IDF 与向量不同尺 → 余弦失去意义）比慢一点更贵。已列入待办。
- 维度仍是 256 而不是倒排索引：倒排能做精确匹配 + 真 BM25，但向量不再定长，要改存储结构。

自测：215 + 30 + 17 + 24 + 24 + 14 + 16 + 26 + 19 = **385**。

---

## [0.9.1] - 2026-11-14

语用层（语言是记忆的一部分）+ 绿灯审计工具。按计划留在 0.9 线上，1.0.0 仍是正式版目标。

### Added

- **`pragmatics.py`（语用层）**：一句话是什么行为、有多硬、为什么说。
  `Speech(act, force, register, motive, hedged, confidence, evidence)`。
  - 行为：禁令 / 要求 / 请求 / 偏好 / 暗示·不满 / 纠正 / 撤回 / 确认 / 自述 / 提问 / 陈述
  - 强度：`基础 × 模糊限定(0.6) × 强调`，并据此建议层级（两级衰减）
  - 场景：review / execution / coding / chat，**看不出就老实返回 chat**
  - 全部规则法 + evidence，可指着原话解释，不引入模型
- **`tools/greenlight_audit.py`**：变异测试体检。12 个变异（衰减、弃答、IDF、否定词、
  风格裁决、作用域归一、双时态、去重、撤回语义、模糊降级、跨项目沉淀、语用合并），
  每个都指定「该由哪些测试发现」。**当前 12/12 被抓，0 漏洞。**
- **`tests/test_pragmatics.py`**（26 项）+ **`docs/PRAGMATICS.md`**。

### Changed

- `remember(..., auto_tier=False)`：默认只**标注**语用档案，不动调用方给的层级；
  `auto_tier=True`（CLI `--auto-tier` / MCP 默认开）才按建议分层。
  刻意不做「自动 pin」——钉死是用户主权。
- `context` 注入块按语用打标：`（禁令）` / `（不满·未确认）` / `（撤回（不等于同意））`。
- `MemoryNode.speech` 参与序列化；合并时**更硬的那次覆盖更软的**。

### 核心语义（这一版真正想守住的）

| 说法 | act | force | tier | is_preference |
|---|---|---|---|---|
| 以后都别用表格了 | prohibition | 0.90 | core | ✅ |
| 别用表格 | prohibition | 0.72 | fact | ✅ |
| 能不能别用表格 | request | 0.55 | event | ✅ |
| 表格是不是有点多 | complaint | 0.35 | event | ❌ |
| 我上次说过别用表格 | correction | 0.85 | event | ❌ |
| 随便你 | withdrawal | 0.25 | event | ❌ |

**「随便你」不是同意。** 把它记成偏好，等于拿到一张用户从未发出的许可。
`withdrawal / question / hint / complaint / correction` 一律不算稳定偏好。

### Fixed（由绿灯审计抓到）

- **假绿断言**：「合并时更硬的那次覆盖更软的」原来用
  「用表格呈现」vs「以后都别用表格了」——两句余弦与长度比都不够，
  **实际根本没走合并路径**，是新建了一条，断言却绿。改成两句文本完全相同、
  只换 speech 对象后，变异 `speech-force-kept` 立刻被抓。
- 测试里还留着一段**安慰剂代码**（`p.classify_enabled = False`，该属性根本不存在），
  已删 —— 它不产生任何效果，只是让测试看起来更严格。

### 版本策略

**暂不推 1.0.0。** 0.9.x 继续消化小版本；1.0.0 的四条硬条件（外部使用者、语义评测集、
跨进程并发、API 冻结）一条都没达成，标 1.0 就是另一种绿灯幻觉。

自测：215 + 30 + 17 + 24 + 24 + 14 + 16 + 26 = **366**，外加 12 个变异体检全抓。

---

## [0.9.0] - 2026-11-14

「词典是基础」——这一版只做词法层，因为上一版那个「输出」误聚的根在这里。

### Added

- **`lexicon.py`（词法层）**，四件事，每件都修一个具体的错：
  1. **归一**：NFKC + 小写。`ＵＴＦ－８` / `utf-8` / `UTF8` 归成同一个词。
  2. **分词**：中文双字 bigram + 标识符（`env.sh` → `env.sh`/`env`/`sh`）+ 单字过滤，
     但**否定词保留**。
  3. **否定词**：`不没无非别勿禁未莫` 单字保留并 ×2 权重。
     「不追高」与「追高」余弦 **0.359**（自身重复 1.000）—— 漏掉它会把结论说反。
  4. **IDF**：`log((N+1)/(df+1))+1`，从语料现算、不落盘。
- **`tests/test_lexicon.py`**（16 项），含一条**反事实**：
  把 IDF 换成全 1，「构建机输出乱码」与「输出用表格」的余弦从 0.448 回到 **0.783**，
  两者会被聚成一类 —— 证明 IDF 真的在拦这件事。
- **`docs/LEXICON.md`**：四个决定、性能账、四条边界。

### Changed

- 字符维 **64 → 256**。64 个桶装几千个词项，碰撞本身就是误聚的第二个来源。
- 字符维从「每对字符等权 +1」改为「词项 × TF 饱和 × IDF」。
- `SemanticIndex` 新增 `encode_query()`：**任何与库内向量比余弦的地方都必须走它**
  （直接用 `encoder.encode()` 得到的是无 IDF 向量，与库内不在同一空间）。
- `recall` 的门槛现在要求「有词法证据」（语义或标签至少一路有贡献）。
  纯 recency 不再算相关：刚写进来的东西不该因为「新鲜」被召回。

### Fixed

- **去重静默失效**：`_find_duplicate` 用无 IDF 向量去比带 IDF 向量，余弦无意义。
  已改成同尺编码（`tests/test_temporal.py` 的去重断言恢复了）。
- **空 IDF 退化成概念单向量**：语料还没建索引时，所有词项权重为 0，向量只剩概念维，
  两条共享概念的记忆余弦会变成 **1.0**。改为「空 IDF 视为无 IDF」。
- **突变审计自己失效**：第 10 幕 patch 的是 `ConceptEncoder.encode`，
  而检索已改走 `encode_with` —— 坏编码器根本没生效。审计报 FAIL 不是它错，
  是它抓到了 patch 点过时。已同时 patch 两条路径。

### 性能（如实记账）

| 指标 | 前 | 后 |
|---|---|---|
| 200 条 `cycle` | 19 ms | 51 ms |
| 1000 条 `cycle` | 202 ms | 561 ms |
| 查询（热） | 1.5 ms | 3.1 ms |

三个缓存把损失从 12 倍压到 2.6 倍：分词结果 LRU（一次 rebuild 里同一文本要算两遍）、
聚类预取向量（原来 N² 对触发 2N² 次 lookup）、维度 64→256（这是正确性修复，不是优化）。

自测：215 + 30 + 17 + 24 + 24 + 14 + 16 = **340**。

---

## [0.8.0] - 2026-11-14

收到一份技术批评，说这库有三处"架构硬伤"。逐条跑代码核查后：
**一条论据被截断、一条结论对但归因错、一条不成立**；但它确实点出了我该补的东西 ——
不是代码，是**可发现性**。所以这一版不加机制，只把已经存在的能力变成契约。

### Added

- **`reflector=` 钩子**：外部反思（可接一次 LLM）。`ReflectionEngine` 默认仍是规则化监控，
  传 `reflector(payload) -> list[str]` 后，观察会以 `〔外部反思〕` 追加进 findings。
  三条护栏：不改配置（拿不到 suggestions）、只追加文字、抛异常只记 `reflector_error`
  不炸穿代谢。payload 含 metrics + 最近 40 条事件 + 自述摘要。
- **`examples/custom_encoder.py`**：默认编码器 vs 注入编码器的真实对比输出。
- **`tests/test_extensions.py`**（14 项）：把两个扩展点的行为变成契约，
  含"坏编码器抛异常时写入必须显式失败""维度不符被拒绝""外部反思不能改配置"。
- **`docs/CRITIQUE_RESPONSE.md`**：三条批评的逐条核查，附实测数据与完整代码引用。

### Changed

- `encoder=` 从"藏在构造函数里"变成**公开扩展点**：README 有新一节，示例 + 测试 + 文档齐备。
  （它从 v0.1 就在，只是没人知道 —— 能力存在但没人知道，等于不存在。）
- `ReflectionEngine` docstring 直说"这是规则化监控，不是 LLM 反思"。

### 核查结论（写进文档，也写在这里）

| 批评 | 结论 |
|---|---|
| 语义聚类只按标签，"完全丧失语义能力" | **论据被截断**：截图止于第 6 行，完整函数第 84–88 行有语义通道，`max(structural, 0.85*cosine)`；实测 0.000→0.773 |
| 同上，"抓不到字面不同但语义相近" | **结论成立，归因错**：不是没有通道，是默认编码器是 bigram（0.057）。已提供 `encoder=` 与示例 |
| 反思引擎是硬编码阈值告警 | **完全成立**，是命名问题：本来就是规则化监控。已加 `reflector=` 钩子 |
| 风格冲突只检出不裁决 | **不成立**：`active_statements` 只返回 `is_effective` 的条目，`suppressed_by` 会挡掉输家，第 3 幕有断言守着 |
| 换 SQLite + WAL 解决并发 | **诊断对、方案不对**：SQLite 解决语句原子性，不解决 `load→改→save` 的整体覆盖；且会丢掉"快照是可读 JSON"这个可调试性。正确解是乐观并发（state 带 revision，save 前比对）。**未实现**，已列待办 |

### 顺带发现的更严重问题（批评没提到）

默认编码器会**误聚**：`"构建机输出乱码"` 与 `"输出用表格"` 共享「输出」二字，
被聚成一类（cy 0.301，标签相同时 0.750 稳过线）。
误聚比漏聚危险 —— 它会写进一条错结论。这也是"换编码器"比"修阈值"更根本的原因。

自测：215 + 30 + 17 + 23 + 24 + 14 = **323**。

---

## [0.7.0] - 2026-11-13

跨项目记忆、DSH 插件、以及能看的面板。

### Added

- **作用域（`scope.py`）**：记忆分 `global`（跨项目：偏好/硬规则/通用教训）与
  `proj:owner/repo`（项目内：架构决策/踩过的坑）。默认视图 `auto` = 全局 + 当前项目。
  `--scope global|project|session|all` 自由切换；`promote` 把项目教训升为通用。
  项目标识按 `--project` → `MEMORY_PALACE_PROJECT` → **git remote** → 目录名+哈希 推断，
  所以换目录、换机器、重新 clone 还是同一个项目。
- **沉淀按作用域隔离**：跨项目的碎片不再被聚成一类 —— 否则会结晶出一条"不属于任何项目"的结论。
- **`projects` 命令**：一眼看出记忆分布在哪些作用域、当前在哪个。
- **DSH 插件（`dsh/`）**：host 半注册 5 个工具（context / remember / recall / promote / status），
  按 `defineTool` 契约写、`node --check` 验过语法；`cordis.yml` 给绝对路径与 env 模板。
  面板不嵌 DSH 打包链，走 `GET /panel`（见下），避免随宿主 rc 版本碎掉。
- **`GET /panel`**：`python -m memory_palace.http_api --port 8765` 后浏览器直接开，
  页面按作用域过滤，含作用域分布、剩余寿命分桶、进出账、卡片、自述、墓碑。

### Changed

- `view` / `render` / `context` / `recall` 全部支持 `--scope`；面板与健康度读数跟随视图
  （看着项目视图却报全库熵，会让人误判注意力很集中，其实是被别的项目摊平了）。
- MCP 工具新增 `memory_promote`，`memory_context` / `memory_remember` 支持 `scope`。
- `text_report` 的总数改为"本视图在册数（库共 N）"，不再只报全库。

### Fixed

- `text_report` 引用未定义的 `scopes`（作用域过滤加进去时漏了参数），`view --scope` 直接崩。
- 只有钉死记忆时 `html_report` 的平均剩余寿命除零。
- `scopes_for` 未归一项目名：传裸名 `repo-a` 与节点上存的 `proj:repo-a` 不相等，
  过滤会**静默失效**（表现为"串味"，但不报错）。

自测：215 + 30 + 17 + 23 + 24 = **309**（含 3 条反事实）。

---

## [0.6.0] - 2026-11-13

看完同类项目之后补的三处**语义漏洞**（不是新功能），以及一份调研结论。

### Added

- **双时态（事实有有效期）**：`MemoryNode` 新增 `valid_from` / `valid_to` / `superseded_by` / `supersedes`。
  `remember(..., supersedes=["旧名"])` 让旧事实**到期**而不是被删：默认检索只给当前有效的，
  `recall(as_of=...)` / `recall --as-of 30d|2026-10-01` 回看"当时为真的是什么"。
  失效节点不进重要性账本、不进结构读数、不参与沉淀；不按价值 GC，而是按
  `history_retention_days`（默认 90 天）回收并进墓碑（仍可 restore）。
  这是 Zep/Graphiti 的核心赌注，本库用四个字段 + 一处过滤就拿到，无新依赖。
- **弃答（abstention）**：`context` 在无关查询下不再把"最重要的两条"端上去凑数，
  而是输出「没有相关记忆，不要凭印象补全」，同时**保留钉死的硬规则**（规则与问题无关也要遵守）。
  判据两层：绝对线 `abstain_semantic_floor`（0.30，实测底噪 ≤0.13 / 真匹配 ≥0.5）+ 相对线
  `semantic_relative_floor`。`--status-only` 会显示「无相关记忆」。
- **写入去重**：`remember(..., merge=True)`（CLI / MCP / HTTP 默认开，库层默认关）——
  余弦 ≥0.92 **且长度比 ≥0.85** 才算重复，合并为"强化 + 标签并集"，不新建节点。
- **`docs/COMPETITIVE_ANALYSIS.md`**：Mem0 / Zep / Letta / Cognee / LangMem / TencentDB Agent Memory /
  progressive-memory / mcp-memory-service 的逐项对照，含"为什么拒绝"与"还缺什么"。
- **`tests/test_temporal.py`**：23 项断言，其中 3 条是**反事实**（把有效期判定改恒真、把弃答线降到 0，
  对应断言必须变红）。

### Changed

- `STATE_VERSION` 5→6，`SUPPORTED_STATE_VERSIONS = (4, 5, 6)`。
  `PalaceConfig.from_dict(strict=...)` 现在按快照版本决定严格度：旧快照缺新字段用默认值补齐，
  本版快照出现未知字段仍然拒绝 —— 两种错误不能混为一谈（旧版会把前者也当坏数据，升级即失联）。
- `context` 的"相关"改由语义/标签判，不再用总分（总分含时近分量，人人有份，判不出无关）。
- MCP / HTTP 暴露 `supersedes` 与 `as_of`；`recall` 返回里带 `retired` / `superseded_by`。

### Fixed

- 去重阈值只按余弦会**静默吃掉新内容**：`"单笔不超过总资金三分之一"` 与
  `"…三分之一，且最多同时持三只"` 余弦 0.927，后者补了新约束却会被合并。加长度比（≥0.85）后分开。
- `view` / `status` 里失效记忆混进重要性排序，看起来像"不太重要"，其实只是过期了 —— 现在单列。

---

## [0.5.1] - 2026-11-13

修 CI 抓到的一个真 bug。

### Fixed

- `view` / `text_report` 在「两条记忆剩余寿命完全相同」时抛 `TypeError`
  （`sorted` 落到比较 `MemoryNode` 对象）。排序键改为 `(天数, 名字)`，并补了回归测试。
  这个 bug 只在演示库 60 天版触发 —— 说明「演示能跑」本身就是一条测试。

---

## [0.5.0] - 2026-11-13

三件事：树状注入（省 token）、性能、以及把「演示」做成能见人的东西。

### Added

- **树状注入（`tree.py` + `context --tree`）**：根常驻、命中的枝展开到叶子、其余折叠成一行摘要，
  并显式标注「另有 N 条」。派生自图（沉淀父节点 / 首个标签），不改 state 版本。
  实测 1000 条：同一覆盖面下扁平 1184 字 → 树 603 字（省 49%），耗时 31ms → 8ms。
  诚实说明：窄预算（只要 6 条细节）时树反而略贵，两者是两个档位而不是替代关系。
- **性能**：① 重要性账本加缓存（一次 context 里原本要算 3~4 遍）；
  ② 语义索引改脏标记，不再每次查询哈希全语料；③ 沉淀候选封顶 + 大规模分桶配对。
  `cycle` 在 1000 条上从 1627ms 降到 200ms。
- **`bench.py`**：写入 / 查询 / 注入 / 代谢 / 存储的基准，并与扁平注入对比 token。
- **MCP `--profile slim`**：只暴露 `context / remember / cycle` 三个工具，给「工具定义常驻上下文」
  的宿主（DSH 等）省约 60% 的 schema token；`--list-tools` 可查。
- **`config` 命令**：`memory-palace config --for dsh|claude|codex|cursor|http` 直接打印可粘贴配置，
  绝对路径与转义都算好。
- **DSH（DeepSeek Harness）适配**：`cordis.patch.yml` 写法、`mcp__memory-palace__*` 工具命名、
  环境变量清洗（含 TOKEN/KEY 的会被剥掉，store 必须显式写进 env）、slim 档建议。
- **演示与配图**：`examples/showcase.py` 生成 120 天演化库（全部脱敏，通用 agent 场景）；
  `examples/make_images.py` 生成 README 配图（真实截图，可复现）。

### Changed

- **面板重做**：连续衰减曲线换成「剩余寿命分桶」与「每周进出账」—— 前者在该数据上是一堆平线，
  什么也没说；卡片两列等高、去掉重复的「可靠度 1.00」、徽章不再重复标「钉死」。
- 内部枚举翻译成人话（`flat` → 无梯度、`isolated` → 全是孤岛）。

### Fixed

- 空库的 `structure_health` 判成 `hub`。
- 演示库用「未来的模拟时间」，导致渲染时与重载时的衰减对不上（锚到真实时间）。
- 进出账的「新增」只统计存活节点，墓碑里的历史被漏掉。

---

## [0.4.0] - 2026-11-13

补上**展现层**。起因是一个很实际的问题：记忆系统改的是 prompt 里的一小段，用户看不见 ——
「用了很久」和「根本没生效」，在体验上一模一样。技能加载后必须让人**看见效果**。

### Added

- **状态行**（`context --status-only`）：`[记忆 6/9 · 钉死 2 · 147/900字 · 自述 rev1]`。
  一行交代「这次用了什么」，数字来自命令输出而非印象。
- **终端视图**（`view`）：重要度条形 + **剩余寿命**（`钉死` / `N天` / `极稳（>5年）` / `已过期`）
  + 「N 天内将跌破遗忘线」+ 已遗忘清单。
- **HTML 面板**（`render --out x.html`）：自包含单文件（无 CDN / 无图片 / 跟随系统深浅色），
  含记忆卡片、**未来 N 天衰减曲线**（钉死的是平线）、注意力份额、自述章节、墓碑。
- **`memory_palace/render.py`**：以上三层的实现，纯函数，不写盘（除 render 输出）。
- **技能的「展现契约」**：注入后一行状态、引用记忆时句末角标 `〔记忆·仓位〕`、cycle 后一行摘要、
  用户问「你记得什么」→ `view`、要「看看」→ `render`。写进 SKILL.md，成为硬性输出要求。

### Fixed

- 空库的 `structure_health` 被判成 `hub`（单枢纽）—— 没有任何节点却说它「全靠一个中心撑着」。
  改为 `empty`，并加测试。
- 身份类记忆的剩余寿命直出 `13659284 天`，改为「极稳（>5年）」。

---

## [0.3.0] - 2026-11-13

不做某个平台的专用件。三个接入面覆盖主流 agent：**MCP（stdio）/ HTTP + OpenAPI / CLI + 库**。

### Added

- **MCP 服务（`memory_palace/mcp_server.py`）**：stdio 传输，换行分隔 JSON-RPC（不是 LSP 的
  Content-Length），8 个工具 —— `memory_context / remember / recall / outcome / forget / status /
  explain / cycle`。工具描述写的是「什么时候该用」，工具级失败用 `isError` 交给模型自行纠正，
  协议错误才用 JSON-RPC error。入口 `memory-palace-mcp`。
- **HTTP + OpenAPI（`memory_palace/http_api.py`）**：`/context /remember /recall /outcome /status
  /cycle /health /openapi.json`，OpenAPI 3.0 可被 Dify / Coze / n8n / GPT Actions 直接导入。
  默认只绑 127.0.0.1 且无鉴权，非本机绑定需显式 `--allow-remote`。入口 `memory-palace-http`。
- **`docs/INTEGRATIONS.md`**：Claude Code（`.mcp.json`）、Codex CLI（`config.toml`）、Cursor、
  Cline、Windsurf、Zed、Gemini CLI、Dify、Coze、n8n、LangChain、OpenAI function calling
  的逐条配置；并说明 `inputSchema` 可直接当 function calling 的 `parameters` 用，不必重抄 schema。
- **`AGENTS.md`**：任何读仓库约定的 agent（Codex / Claude Code）都能照着用的时序与硬约束。
- **`tests/test_integrations.py`**：15 项协议层契约（握手 / 工具清单 / 错误语义 / OpenAPI 形状 / 404 与 400 的区分）。

### Changed

- CLI 的 `context` 排序改为三档：钉死永远在 → 两条背景 → `0.6×语义 + 0.4×重要性`。
  修掉「无关查询下硬规则被语义门槛挡掉」的问题 —— 那是规则变成资料的典型退化。
- `pyproject` 增加 `memory-palace-mcp` / `memory-palace-http` 两个入口。

### Fixed

- `context` 在语义门槛较高时只返回 1~2 条，导致注入块几乎没有信息量。

---

## [0.2.0] - 2026-11-13

从「库」到「agent 能用的工具」。核心判断：agent 缺的不是相似文档，而是
「我该记住什么、该放下什么、这条还能不能信」，所以接口不是 `search() -> chunks`，
而是 `context(task, budget) -> 一小块可注入的文本`。

### Added

- **CLI（`memory_palace/cli.py`）**：`init / remember / recall / context / cycle / status / explain /
  outcome / pin / forget / style`。默认存储 `~/.memory-palace/store.json`，
  可用 `--store` 或 `MEMORY_PALACE_STORE` 覆盖；`pip install` 后入口为 `memory-palace`。
- **`context` 三档排序**：钉死记忆永远在（规则不是资料）→ 两条背景（当前最重要）→
  其余按 `0.6×语义 + 0.4×重要性` 混合。带可信度警示、风格约束、预算用量，
  `--json` 可拿结构化结果。
- **`MemoryPalace.pin() / drop()`**：用户主权的两个开关。`drop` 走与 GC 相同的清理路径
  （墓碑 → 出图 → 出语义索引 → 剪逻辑边），不留幽灵。
- **`docs/AGENT_INTEGRATION.md`**：三种接入时机（注入 / 固化 / 反馈）、多库划分、成本，以及**不该用它**的场景。
- **`tests/test_cli.py`**：16 项契约自测，CI 增加一步。

### Changed

- `retrieval_coverage` 在「一次检索都没有」时返回 1.0 而不是 0.0，并新增 `retrieval_attempts` 指标；
  `retrieval_narrow` 规则要求 `retrieval_attempts >= 5` 才触发。
  **修掉的真实副作用**：新库第一轮 `cycle` 会误判「召回路径过窄」，去下调语义门槛。

### Fixed

- 空库/新库跑第一轮 `cycle` 会凭空产生一条自修正（零样本被当成零覆盖率）。

---

## [0.1.0] - 2026-11-13

首个公开版本。核心是「记忆的生命周期」，不是「检索的一次调用」。

### Added

- **核心（`core.py`）**：幂等衰减（权重 = 锚点 + 时间的纯函数，GC 跑 1 次与 100 次账本一致）、
  共现图、原子持久化（tempfile + fsync + `os.replace` + 目录 fsync，含 Windows 路径）、
  状态版本 5，兼容读取 v4 快照。
- **情感（`affect.py`）**：潮水（唤醒）/ 河床（峰值唤醒）/ 失真代价三量分离；
  `fidelity` 随峰值唤醒下降且不随时间恢复；评价器从文本词表与运行时结果推断情绪，带 `source` / `drivers`。
- **逻辑（`logic.py`）**：有向带类型关系、传递闭包、承重打折、矛盾沿推理链传播、扩散激活带扇出惩罚。
- **沉淀（`consolidation.py`）**：标签 Jaccard 与语义余弦双通道聚类，单向门（结晶后碎片降级 trace）。
- **选择（`value.py`）**：重要性四分量（强度 / 情绪余温 / 逻辑凭据 / 叙事地位）、
  两套遗忘律（结论按价值、碎片按物理强度）、固定注意力预算、每条判决带理由；
  新增**结构熵**（度熵 / 孤立占比 / 连通分量 / 碎片度），与价值健康分开判。
- **自述（`narrative.py`）**：投影而非节点；章节与叙事弧；条目级不可靠标注；叙事地位随时间退潮。
- **人格（`persona.py`）**：基座 / 人格 / 人设 / 修订四层；基座只读（修宪需 reason + force）、
  人格带惯性与包络硬夹、人设可换可回滚、漂移判据 = 距离 × 速度。
- **记忆模型（`memory_model.py`）**：五种记忆五种寿命；工作记忆超容量**降级**不删除；身份记忆不参与遗忘。
- **反思（`reflection.py`）**：指标化回看，只提建议；新增结构（孤岛 / 碎裂 / 单枢纽）与注意力未覆盖率读数。
- **自修正（`selfcorrect.py`）**：12 个白名单参数 + 逐参数值域 + 单次步长 ≤30% + 冷却 + Patch 可回滚；
  新增观察窗采样，**样本比观察窗还旧时拒绝判决**；判据要求变化幅度跑赢噪声带；同参数回滚 ≥2 次永久收权。
- **语义（`semantics.py`）**：多义词按上下文分配概念份额（不再硬编码归属）；
  概念词从语料学习（df 区间 + 与同概念 ≥2 个锚点同篇 + 提升度 + 纯净度）；
  `semantic_explain()` 可查「为什么归到这个概念」。索引状态是图内容的纯函数，跨进程逐条一致。
- **示例与自测**：`examples/quickstart.py`；`memory_palace/demo.py` 21 幕剧本，215 项断言，
  18 条突变审计（故意弄坏实现，断言必须变红）。

### Changed

- `MemoryPalace.remember(valence=None, arousal=None)`：不传情绪时走评价器，而不是默认全零。
  **行为变化**：文本含情绪词（亏 / 突破 / 恐慌…）的记忆会自带情绪，进而更难忘、也更不可靠。
- `MemoryNode.narrative_hits` 语义收窄为「审计用累计次数」；价值判断改读会退潮的 `central`。
- `atomic_write_json` 返回布尔值（目录项是否持久化），不再静默降级。
- `STATE_VERSION` 4 → 5。

### Fixed

- 没有自述就 `reflect()` 会因 `float(None)` 崩溃。
- Windows 上目录 fsync 永远失败并被静默吞掉（改为 `CreateFileW + FlushFileBuffers`）。
- `replace` 与临时文件清理同时失败时会残留 1 个 `.palace-*.tmp`（改为重试 + 下次写盘回收）。
- 叙事地位单调饱和：讲过一次的记忆终身「重要」。
- `资金` 在「板块资金接力」语境里被误判为仓位概念。

### Known Limitations

见 [README · 成熟度自评](README.md#成熟度自评先说不好听的)。要点：语义层不是 embedding；
测试是作者自证；自修正的「变好」仍是单指标；单写者假设。
