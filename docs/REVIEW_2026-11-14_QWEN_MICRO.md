# Qwen 微优化建议：五条逐条核对

> 2026-11-14 收到第二份评审（`optimization_suggestions.html`），针对 v0.11+ 的五个「微优化」点。
> 这次它读的代码是新的，事实基本对得上。逐条跑代码验证后：**两条采纳、一条部分采纳、一条已实现、一条不采纳**。
>
> 结论先行：五条都没有架构级问题，收益全是常数级。真正值钱的是第 1 条的反面 ——
> 它建议的「快速拒绝」会**悄悄关掉语义聚类唯一能发现同义碎片的路径**。

---

## 1. 沉淀聚类的快速拒绝 —— ❌ 不采纳（会改变行为）

**它说的**：`consolidate_similarity > 0` 时，没有共享标签的节点对 Jaccard 必为 0，
永远不可能过线，应该直接 `continue`。

**为什么不对**：本项目的相似度**不是** Jaccard，而是两条通道取较大者：

```python
structural = 0.75 * jaccard + 0.25 * coactivation
return max(structural, 0.85 * cosine(va, vb))   # ← 这一行
```

无共享标签时 `structural = 0`，但余弦仍可能过线 —— 这正是「同一件事换了说法」
唯一能被发现的方式。`_pairs()` 的 docstring 里写得很直白：小规模时**故意**全对比较，
因为「分桶会把换了说法的碎片拆开」，而第 2.5 幕演示的就是这件事。

按它改的后果：语义聚类退化成「只按标签聚合」，且**没有任何测试会红**，
因为你删掉的是一条「本来就不该命中」的路径。这类改动最危险。

**采纳它的替代方案**（省同样的钱，不改行为）：

| 改动 | 原因 |
|---|---|
| `_pairs()` 改生成器 | 600 个候选 = 17.9 万对，先建列表再迭代一次，白占约 2 MB、多一遍构造 |
| `_clusters()` 预取 `frozenset(node.tags)` | 同一节点的标签集合会被重建上百次 |
| 新增 `_similarity_sets()` | 与 `_similarity` 同语义，只是标签集合由调用方给 |

实测（600 条候选 / 29700 对）：**36.8 ms → 32.9 ms**（-11%），且不再构造 2.97 万个元组。

为了防「两份实现漂」，新加了逐对对拍：`test_similarity_sets_matches_similarity`
（24 个节点 × 2 种阈值 × 含余弦路径，>500 组，误差 < 1e-12）。

---

## 2. 反思事件窗口的二分查找 —— ⚠️ 不采纳 bisect，采纳「少遍历」

**它说的**：`events` 严格按时间追加，用 `bisect_left` 把 O(N) 降到 O(log N)。

**为什么不用 bisect**：`events` **不保证单调**。`restore()` 会把旧事件读回来，
测试里用 `MutableClock` 推进时间，机器时钟也可能回拨。对非单调序列做二分，
不报错，只给出一个**错误的切片** —— 这是最坏的一类 bug：反思的窗口悄悄少了一段，
指标看起来仍然合理。

要安全用二分，得先验证有序（O(N)）或维护独立的时间戳索引（得在每次 append/restore/trim 时同步）。
在一个窗口上限 5000 条、单次 1.94 ms 的场景里，这份复杂度不值得。

**采纳它想达到的目的**：窗口过滤与统计合成一次遍历。

原来：建窗口列表（1 遍）+ `count(kind)` 6 遍 + payload 求和 3 遍 ≈ 10 遍。
现在：一遍循环里同时过滤、计数、累加 payload。

实测（5000 条事件，单次 `run()`）：**1.94 ms → 1.16 ms**（-40%）。

---

## 3. 指标统计的单次遍历 —— ✅ 采纳（比建议更彻底）

它建议 `Counter(e.kind for e in window)` 再查表。但 `gc_deletes`、`total_hits`、
`semantic_recalls`、`scores` 仍然要各自再遍历一遍。

实际做法：在同一个循环里同时做计数与 payload 累加，连 `Counter` 的对象都不用建。
见上一条的实测数字。

---

## 4. Dataclass 字段名缓存 —— ✅ 已实现（0.9.3）

它给的代码：

```python
@cache
def dataclass_field_names(cls: type) -> set[str]:
    return {f.name for f in dc_fields(cls)}
```

仓库里早在 0.9.3 就是这样（`core.py`）：

```python
@lru_cache(maxsize=None)
def _field_names_cached(cls: type) -> frozenset[str]:
    return frozenset(f.name for f in dc_fields(cls))
```

当时的实测依据写在 docstring 里：反序列化一条记忆要问 20 多次「这个类有哪些字段」，
1000 条就是 2.5 万次 `dataclasses.fields()`，读库 136 ms 里有 149 ms（含重复计数）花在这上面。

两个细节差异：用 `frozenset` 是为了防止调用方原地改缓存（缓存返回可变对象是经典坑）；
用 `lru_cache(maxsize=None)` 而不是 `cache`，是为了兼容 3.10（`cache` 是 3.9+ 的别名，行为相同）。

---

## 5. 配置修改的鲁棒性回滚 —— ✅ 采纳

**它说的**：`apply_suggestion` 只捕获 `ValueError`，其他异常会让内存停在未定义状态。

这条对。原来的写法：

```python
setattr(self._config, param, value)
try:
    self._config.validate()
except ValueError:
    setattr(self._config, param, old)
    raise
```

`validate()` 现在只会抛 `ValueError`，但那是**实现细节不是契约**。若将来某个校验分支
漏出 `TypeError`，内存里就会留下一个「没校验过、也没落盘」的参数 —— 重启后静默变回去，
调参的人会以为是自己的记忆出了问题。

现在：`setattr` 与 `validate` 一起放进 `try`，`except Exception` 回滚后**原样抛出**（不吞）。
新增 `test_apply_suggestion_rolls_back_on_any_exception`：让 `validate` 抛 `TypeError`，
断言参数回到原值、异常照常冒出来。

注意这不是「捕获所有异常」式的掩盖：异常仍然向上抛，只是保证状态一致。

---

## 汇总

| # | 建议 | 判定 | 实际改动 | 实测 |
|---|---|---|---|---|
| 1 | 聚类快速拒绝 | ❌ 会关掉语义聚类 | 预取标签集合 + `_pairs` 改生成器 | 36.8 → 32.9 ms（600 候选） |
| 2 | 反思 bisect | ⚠️ bisect 不安全 | 窗口过滤与统计合并为一遍 | 1.94 → 1.16 ms（5000 事件） |
| 3 | Counter 合并 | ✅ 更彻底地采纳 | 单遍循环同时计数与累加 | 同上 |
| 4 | 字段名缓存 | ✅ 0.9.3 已实现 | 无需改动 | — |
| 5 | 异常回滚 | ✅ 采纳 | `except Exception` + 原样抛出 + 测试 | — |

**收益的量级要说清**：全部加起来是「每轮 cycle 省几十毫秒」，不是数量级提升。
写在这里是因为**常数优化也要有数字**，否则「优化过了」只是一句话。

**没有采纳的部分也留档**：第 1 条如果照做，测试仍然全绿 —— 这正是它危险的地方。
代码评审里最该警惕的不是「改错了会红」，而是「改错了也不会红」。

---

## 附：这轮改动差点让审计漏两个

采纳第 1 条之后，绿灯审计从 **30/30** 掉到 **29/31** —— 新加的变异一个没抓住，
还顺带把原来的 `early-exit-wrong` 弄丢了。原因有两个，都值得记住：

1. **为省一次 set 而复制算术**：`_similarity_sets` 与 `_similarity` 各有一份 `max(structural, 0.85*cosine)`，
   变异只改到其中一份，另一份还在，测试自然不红。
   现在 `_similarity` 委托给 `_similarity_sets`，**只有一份实现**。
2. **反事实打错了对象**：早停测试原本 patch `engine._similarity` 来构造「永远算余弦」的对照组，
   而 `_clusters` 已经直接调 `_similarity_sets` —— 补丁没生效，两次跑的是同一段代码，
   结论「簇结构一致」当然成立。现在改成 patch `_similarity_sets`，并加了一条
   「向量查表次数必须更少」的断言，**证明早停真的被走过**，而不是两次都走了同一条路。

教训：**优化如果靠「复制一份再改快」，就要同时付出「守住它」的成本**；
而反事实测试必须验证「差异真的发生了」，否则它只是在自证清白。
