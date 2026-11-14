# 三条批评的逐条核查（以及两个扩展点）

> 2026-11-14 收到一份技术批评，指出三处"架构硬伤"。
> 逐条跑代码验证的结果：**一条论据被截断、一条结论对但归因错、一条不成立**。
> 同时它确实指出了我该补的东西 —— 不是代码，是**文档与可发现性**。

---

## 一、核查：语义聚类是不是"伪智能"？

### 批评原文引用的代码（截图止于第 6 行）

```python
def _similarity(self, a, b, graph) -> float:
    ta, tb = set(a.tags), set(b.tags)
    jaccard = len(ta & tb) / max(1, len(ta | tb))
    edge = graph.edge(a.name, b.name)
    co = min(5, edge.coactivations if edge else 0) / 5.0
    return 0.75 * jaccard + 0.25 * co          # ← 截图到这里结束
```

### 实际代码（`consolidation.py:73-88`）

```python
    structural = 0.75 * jaccard + 0.25 * co
    if self.vector_lookup is None:
        return structural
    va, vb = self.vector_lookup(a), self.vector_lookup(b)
    if not va or not vb:
        return structural
    return max(structural, 0.85 * cosine(va, vb))   # ← 语义通道在这里
```

`palace.py:97` 确实注入了 `vector_lookup=self._vector_of`。
**"系统完全丧失了处理字面不同但语义相近的能力"不成立** —— 语义通道存在且在跑。

### 但结论有一半是对的，实测数据如下

| 场景 | jaccard | cosine | 纯标签口径 | 实际（含语义） | 会聚吗 |
|---|---|---|---|---|---|
| 标签不同 + 字面几乎相同 | 0.00 | 0.909 | 0.000 | **0.773** | ✅ 靠语义聚到 |
| 标签不同 + 同义改写 | 0.00 | 0.057 | 0.000 | 0.049 | ❌ **聚不到** |
| 标签相同 + 内容无关 | 1.00 | 0.301 | 0.750 | 0.750 | ⚠️ **会误聚** |
| 标签相同 + 同义 | 1.00 | 0.120 | 0.750 | 0.750 | ✅ 靠标签 |

三件事同时成立：

1. **语义通道是活的**（第一行 0.000 → 0.773），截断截图掩盖了这一点。
2. **但它抓不到同义改写**（第二行 0.057）。原因是默认编码器是「概念种子 + 字符 bigram」，
   不是 embedding。这是**取舍**（零依赖、离线、可解释），不是架构缺陷。
3. **最危险的是第三行**：标签相同的无关内容会以 0.750 稳稳聚成一类。
   批评者说它"退化为标签分组器"——实际上比这更糟：**它会误判**。
   实测 `"构建机输出乱码"` 和 `"输出用表格"` 因为共享「输出」二字被聚成一类
   （见 `examples/custom_encoder.py` 的真实输出）。误聚比漏聚危险，因为它会写进一条错结论。

### 所以真正该做的是：让默认实现被替换，而不是继续解释它

`encoder=` 参数**从第一版就在** `MemoryPalace.__init__` 里，但它没文档、没示例、没测试。
能力存在但没人知道，等于不存在 —— 这是可发现性问题，我认。

现在补上：

```python
from memory_palace import MemoryPalace

class BgeEncoder:
    dim = 512
    def encode(self, text: str) -> list[float]:
        return model.encode(text, normalize_embeddings=True).tolist()

palace = MemoryPalace(encoder=BgeEncoder())
```

**换一个编码器，沉淀与检索同时生效**（不是只改一半）：

```
默认编码器：  同义改写 cosine=0.057 → 聚不到
注入之后：    同义改写 cosine=0.949 → 聚成一类，检索也能召回
```

见 `examples/custom_encoder.py` 与 `tests/test_extensions.py`（6 条断言守这个契约）。

---

## 二、核查：反思引擎是不是"硬编码阈值告警"？

**这条完全成立。**

`reflection.py` 里没有一行调用 LLM 的代码，全是 `if metrics[...] < 0.5: findings.append(...)`。
它做的是**基于规则的监控**（heuristic monitoring），不是"反思"（reflection）。
我在 README 里写的"指标化回看"是准确的，但"反思"这个词容易被读成认知意义上的反思 ——
**这是命名与定位的问题，不是实现的问题**。

真正的反思要能读事件序列、自己提假设（"我最近总在同一类查询上失败，因为标签打得过细"）。
这类能力必须靠模型，而模型意味着依赖 —— 与"零依赖"冲突。

所以：**加钩子，不改默认**。

```python
def llm_reflect(payload):
    # payload = {"metrics": {...}, "recent_events": [...], "nodes": int, "narrative": {...}}
    return ask_llm(
        "以下是记忆系统的指标与最近事件，指出我可能没注意到的模式："
        + json.dumps(payload, ensure_ascii=False)
    )

palace = MemoryPalace(reflector=llm_reflect)
```

三条护栏（都有断言）：

1. **只追加观察**，写进 `findings` 并标 `〔外部反思〕`；
2. **不能改配置** —— 它拿不到 `suggestions`，参数仍由规则与自修正管。
   让模型直接改参数，等于让它改自己的人格；
3. **失败不炸穿** —— 抛异常只记 `reflector_error` 进 snapshot，一轮代谢照常完成
   （与 `autosave` 同一原则）。

不传 `reflector` 时，行为与之前**完全一致**。

---

## 三、核查：风格冲突是不是"只检出不裁决"？

**这条不成立。**

```python
def active_statements(self, now):
    return [r.statement for r in self.rules.values() if self.is_effective(r, now)]

def is_effective(self, rule, now):
    if rule.suppressed_by is not None:
        return False          # ← 裁决的输家在这里被挡掉
    ...
```

`adjudicate()` 每轮从零重算所有对立对，输家写 `suppressed_by`，`active_statements` 只返回
`is_effective` 的条目。**对立的两条不会同时出现在 system prompt 里。**

`demo.py` 第 3 幕有专门的断言守着这条；`active_conflicts()` 恒空是写死的不变量。
批评里说的"两者同时生效"，与当前实现不符 —— 可能是看了旧版本，或没读 `style.py`。

裁决规则本身是刻意的：用户钉死 > 证据强度 > 容差带（差距落在容差带内时**两条一起停用**）。
最后这条不是"不裁决"，是"证据分不出高下时不硬选"—— 硬选一个就是把噪声固化成风格。

---

## 四、关于替换 SQLite 的建议：不做，但不是因为懒

批评说 atomic_write_json 解决不了多进程并发写（Last Write Wins），建议换 `sqlite3 + WAL`。

**问题诊断是对的，方案不对。**

1. **SQLite 解决的是"写事务"，不是"读-改-写"**。本库的写模式是
   `load → 改内存 → save 整个快照`，SQLite 只能保证单条语句原子，
   两个进程各自 load 完再 save，照样后写覆盖先写 —— 除非把整个数据模型改成行级更新。
2. **换存储会丢掉现在最值钱的性质**：快照是一个可读 JSON，能 `git diff`、能手改、
   能直接看一眼发生了什么。这个可调试性在个人工具里比并发吞吐重要得多。
3. **代价与收益不匹配**：单用户场景下，真正的痛点不是"两个进程同时写"，
   而是"我忘了另一个窗口还开着"。这种情况用**乐观并发**（state 里带 revision，
   save 前比对磁盘 revision，不一致就报冲突）比换数据库便宜二十倍，而且更安全
   —— 它不会静默覆盖，会明确说"别的进程改过了，重载后再试"。

**当前状态：未实现。** 已列入待办，优先级低于组件级评测集。
> **0.10.0 已改**：这一节记录的是当时的结论。现在默认 SQLite 后端，
> 多进程写同一份记忆已支持（见文末「0.10.0 追加」与 `docs/STORAGE.md`）。

---

## 五、这份批评最该被记住的一句

> "代码写得好"不等于"架构无缺陷"。

这句我接受。但它反过来说也成立：**"架构有妥协"不等于"实现是假的"**。
本库的妥协是明码标价的 —— 零依赖换来语义弱、单文件换来并发弱、可解释换来不可规模化。
真正的错误不是有妥协，是**妥协没写在用户能看到的地方**。

所以这次改的不是架构，是三处"能力存在但没人知道"：

| 之前 | 现在 |
|---|---|
| `encoder=` 藏在构造函数里 | `examples/custom_encoder.py` + 6 条契约断言 + README 一节 |
| "反思"是规则但没说明 | docstring 直说"这是规则化监控"，并给 `reflector=` 钩子 |
| 并发限制只在 README 角落 | 上面第四节写清"为什么不换 SQLite"与正确解法 |

---

## 附：这次的实测数据怎么复现

```bash
python examples/custom_encoder.py     # 默认 vs 注入编码器的真实输出
python tests/test_extensions.py       # 14 项断言，含"坏编码器不炸"与"外部反思不改配置"
```

---

# 0.10.0 追加：对「成熟度自评」那张表的逐条回应

上一轮的自评是这么写的（原文摘录）：

> **不适用**：生产级记忆后端、向量检索替代品、存不可再生数据、多进程并发写同一份记忆。
> 五个最大风险：① 语义层不是 embedding；② 测试通过 = 我写的断言通过；
> ③ 自修正的「变好」仍是单指标；④ 单写者；⑤ 没有组件级评测集。

这五条我都认。0.10.0 没有辩解，而是**逐条做成可验证的东西**。
下面是每一条的现状、证据、以及仍然不成立的部分。

## ① 语义层不是 embedding

**做了什么**：新增 `embedding.py`。`SemanticIndex` 多了一条稠密通道，可挂：

- `HashingEmbedder`：有符号特征哈希 + 字符 n-gram + 子线性 TF，512 维，零依赖、跨进程确定；
- `HttpEmbedder`：任何 OpenAI 兼容 `/v1/embeddings`（本地 BGE / vLLM / 云端都算），带批量、缓存、超时。

**证据**（`memory-palace eval --channel hybrid`，k=5，finance 数据集）：

| 通道 | recall@5 | 换说法 recall@5 |
|---|---|---|
| 词法 | 0.725 | 0.591 |
| 混合 | 0.800 | **0.727** |

**仍然不成立的部分**：内置哈希仍然读字面。它补的是字面变形，不是同义跳转。
「止盈」和「落袋为安」它照样连不起来 —— 那要真模型。所以我在 README 里写的是
「可替换的稠密通道」，不是「向量数据库替代品」。

## ② 测试通过 = 我写的断言通过

**做了什么**：新增组件级评测集 `evaluation.py`，两个领域（A 股复盘 / 工程运维）、
80 条记忆、42 条查询、四类标注（检索 gold / 弃答 / 去重对 / 沉淀簇）。

**关键差别**：它的结论**能推翻我的判断**，而且已经推翻了一次 ——

我原先在配置注释里写：「无关查询底噪 0.13 以内，真匹配 0.5 以上，所以弃答线取 0.30」。
标注集一测：0.30 在短查询上误弃 5 条，F1 只有 0.5；平台区是 0.18~0.21，F1 0.93。
原因是那句「真匹配 0.5 以上」是拿**长记忆**当查询测出来的，短查询根本到不了。
默认值已改成 0.20。

断言做不到这件事：断言只能验证「我以为的」，标注集能验证「实际发生的」。

**仍然不成立的部分**：标注是**一个人标的 80 条**。它能验证流程与量级，
不能替代你自己的语料评测。所以 `calibrate` 才是这个功能真正的交付物 ——
它是给「换一批语料」用的，不是给「信我的默认值」用的。

## ③ 自修正的「变好」仍是单指标

**做了什么**：`Patch` 现在带护栏快照（应用那一刻的其它指标），判决要求主指标改善
**且**没有护栏指标相对恶化超过 15%。越界就回滚，并把越界的键与幅度写进
`patch.guard_hits`，`status()["guardrail_reverts"]` 可查。

指标方向是显式声明的（`GUARD_DIRECTIONS`），因为方向弄反会得到一个
「越改越差才留下」的系统 —— 比没有护栏更糟，所以有专门的测试守着方向。

**测试**（`tests/test_guardrails.py`）：主指标 0.5 → 0.9（大幅改善）、
但 `forget_pressure` 0.2 → 0.9，必须退回；只恶化 5% 则必须保留（防噪声触发）。

## ④ 单写者

**做了什么**：

1. `BEGIN IMMEDIATE`（不是 `BEGIN`）：第一毫秒就声明写意图，不给锁升级留窗口；
2. **改之前先重载**：拿到写锁先比 `meta.revision`，比自己新就把别人的提交读进来；
3. **行级增量**：只 upsert `_dirty` 的那些行，别人写别的节点互不干扰；
4. 冲突记账：`SaveResult.rebased` 进 `conflicts` 计数。

**证据**：`tests/test_store.py::test_multiprocess_no_lost_updates`
—— 4 个进程 × 15 条独立写入 + 15 次 merge 同一条记忆，
条目一条不少、`access_count` 恰好等于调用次数、`fsck` 通过。每次 CI 都跑。

**仍然不成立的部分**：SQLite 是「单写多读」，多写者会排队。
写热点场景的吞吐上限就是一个写者，这是设计不是 bug。README 与 `docs/STORAGE.md`
都把这条写在显眼处：要更高写吞吐请换服务端数据库。

## ⑤ 没有组件级评测集

见 ②。补充一句：报告里保留了**不利数字**——去重 F1 0.667、沉淀 F1 0.000。
把坏数写进 README 不是因为自虐，是因为一个只报好数的评测集，
和没有评测集的区别只在于它更会骗人。

## 另外：那张表里还有一条「可维护性 6/10」

没修，也没打算糊过去。`palace.py` 1800+ 行、`demo.py` 2100+ 行照旧；
新模块各自 300~700 行，边界清楚。拆 `palace.py` 是一次大规模重构，
在没有第二个使用者的前提下做它是给自己找麻烦 —— 这一条会等到 1.0 前再做。

## 复现这一切

```bash
python -m pytest tests -q                                  # 97 项
python -m memory_palace.demo                               # 全剧本 + 突变审计
python -m memory_palace.cli eval --channel hybrid          # 组件级评测
python -m memory_palace.cli calibrate                      # 阈值平台
python -m memory_palace.bench 1000                         # 写延迟 p50/p95
python -m pytest tests/test_store.py -q                    # 4 进程并发
```
