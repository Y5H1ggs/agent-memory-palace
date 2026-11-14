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
在那之前，事实就是：**单写者**。多宿主请给每个宿主配不同的 `MEMORY_PALACE_STORE`。

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
