"""semantics.py - 可解释的语义检索层（零第三方依赖）。

不是随机投影，也不是把词袋哈希叫「语义」：
  · 概念层：把同义表述投影到同一维度（凯利 / 半凯利 / 资金管理 / 下注比例 → position）
  · 字面层：字符 bigram 哈希捕捉未登录词，提供区分度
  · 两层加权后 L2 归一化，用余弦相似度检索

向量只从 content 编码，tags 走独立的标签通道 —— 这样「内容相关但标签不同」
才能真正被召回，也才能证明检索读的是语义而不是标签。

这一版解决的是「词典是手搓的」这个硬伤，分两步：

  1. 多义词**不再硬编码归属**。「资金」既属于仓位（资金管理）也属于板块（资金接力），
     以前把它写进 position，于是「板块资金接力」这种查询会被仓位记忆蹭到。
     现在它进 AMBIGUOUS_TERMS，编码时先看上下文（同句里还有哪些无歧义概念词），
     再按份额分给候选概念；上下文为零时按保底份额均分，而不是拍给得分高的那个。
  2. 词典**能从语料里长**。learn_concepts() 用「与同一概念的 ≥2 个不同种子词同篇出现」
     且提升度足够作为判据，长出种子之外的概念词；频率太高（满语料都是）的词被排除，
     否则「记录」这类词会把所有主题连成一坨。学到什么、凭什么学到，explain() 可查。
"""
from __future__ import annotations

import functools
import hashlib
import math
import operator
from collections import Counter, defaultdict
from typing import Any, Callable, Iterable

from .core import MemoryGraph, MemoryNode
from .embedding import DenseIndex, Embedder, EmbeddingError, make_embedder
from .lexicon import NEGATION_BOOST, NEGATIONS, IdfModel, cached_tokens

DEFAULT_CONCEPTS: dict[str, tuple[str, ...]] = {
    "position": ("仓位", "凯利", "半凯利", "资金管理", "下注", "分散", "持仓比例", "单笔"),
    "review": ("复盘", "总结", "回看", "检讨", "事后", "记录"),
    "sector_flow": ("板块", "资金接力", "主线", "连板", "龙头", "共振", "题材"),
    "presentation": ("表格", "分点", "排版", "输出", "格式", "措辞", "精简", "展开"),
    "risk": ("风险", "波动", "回撤", "概率", "不确定", "禁止"),
    "hard_rule": ("硬规则", "权限", "约束", "必须", "不许"),
    "style": ("风格", "偏好", "习惯", "固化", "反证"),
}

# 一个词属于多个概念时，交给上下文裁决；这里只声明「它可能属于谁」，不声明「它属于谁」。
AMBIGUOUS_TERMS: dict[str, tuple[str, ...]] = {
    "资金": ("position", "sector_flow"),
    "止损": ("risk", "position"),
}

# 字符项用哈希桶落位，桶太少就会「撞车」：两个毫不相干的词落到同一维，
# 余弦凭空变高。256 个桶时，「量子计算的退相干时间是多少」对一条金融记忆
# 能打出 0.455 —— 两者一个共享词项都没有，那是纯碰撞。1024 个桶下同一查询
# 只剩真正的「时间」匹配（0.219）。稀疏向量不因维度变大而变重，这里没有理由省。
CHAR_DIM = 1024
CONCEPT_W = 1.0
CHAR_W = 0.22
# 多义词的权重低于无歧义词：它本来就不该单独决定一条记忆属于什么主题。
AMBIGUOUS_W = 0.8
AMBIGUITY_FLOOR = 0.15

Encoder = Callable[[str], list[float]]

_INF = float("inf")
_NEG_INF = -_INF


class SparseVector:
    """只存非零分量的向量。

    为什么值得为它单独一个类型：向量是 263 维，但一条记忆平均只有 18 个非零分量。
    稠密点积每次都要跑满 263 次乘加（实测 12.2µs），按非零分量查表只要 1.3µs。
    检索、聚类、去重三条路径都以余弦为主成本，这一步是量级差别，不是微调。

    为什么不直接用第三方向量库：零依赖是这个项目的第一条约束，而这里要的东西
    只有「点积 + 归一化」两件事，不值得为它引一棵依赖树。
    """

    __slots__ = ("idx", "val", "dim", "_map")

    def __init__(
        self,
        idx: tuple[int, ...],
        val: tuple[float, ...],
        dim: int,
        _map: dict[int, float] | None = None,
    ) -> None:
        self.idx = idx
        self.val = val
        self.dim = dim
        # 预先建一张 分量号 → 值 的表：点积时可以直接查，不必每次重建
        self._map = _map if _map is not None else dict(zip(idx, val))

    @classmethod
    def from_dense(cls, vec: list[float], dim: int | None = None) -> "SparseVector":
        pairs = [(i, v) for i, v in enumerate(vec) if v]
        return cls(
            tuple(i for i, _ in pairs),
            tuple(v for _, v in pairs),
            len(vec) if dim is None else dim,
        )

    @classmethod
    def zero(cls, dim: int) -> "SparseVector":
        return cls((), (), dim)

    def to_dense(self) -> list[float]:
        out = [0.0] * self.dim
        for i, v in zip(self.idx, self.val):
            out[i] = v
        return out

    def dot(self, other: "SparseVector") -> float:
        if self.dim != other.dim:
            raise ValueError(f"语义向量维度不一致: {self.dim} vs {other.dim}")
        if len(self.idx) > len(other.idx):
            self, other = other, self
        them = other._map
        total = 0.0
        for i, v in zip(self.idx, self.val):
            w = them.get(i)
            if w is not None:
                total += v * w
        return total

    def __len__(self) -> int:
        """维度，不是非零个数 —— 要让「像序列一样用它」的调用方不出意外。"""
        return self.dim

    @property
    def nnz(self) -> int:
        """非零分量个数。判断「这条向量是不是空的」用它，别用 len()。"""
        return len(self.idx)

    def __iter__(self):
        """按稠密值迭代：有人会写 `for v in vec` / `sum(vec)`，让它照常能跑。

        代价是摊开成 dim 个值。热路径**不要**用它 —— 那条路走 dot()。
        """
        return iter(self.to_dense())

    def __bool__(self) -> bool:
        return bool(self.idx)

    def __repr__(self) -> str:
        return f"SparseVector(dim={self.dim}, 非零={len(self.idx)})"


def _as_dense(vec: Any) -> list[float]:
    return vec.to_dense() if isinstance(vec, SparseVector) else list(vec)


def cosine(a: Any, b: Any) -> float:
    """余弦相似度。两端都可以是稠密列表或 SparseVector。

    保留稠密入口是刻意的：自定义 encoder 返回的是列表，测试与外部调用方也在用列表。
    内部热路径走稀疏分支，两者必须在数值上一致 —— 这条由 test_lexicon 的对照断言守着。
    """
    if isinstance(a, SparseVector) and isinstance(b, SparseVector):
        return max(0.0, min(1.0, a.dot(b)))
    da, db = _as_dense(a), _as_dense(b)
    if len(da) != len(db):
        raise ValueError("语义向量维度不一致")
    # fsum(map(mul)) 全在 C 层跑，比 sum(x*y for ...) 的生成器快 2.3 倍
    return max(0.0, min(1.0, math.fsum(map(operator.mul, da, db))))


def cjk_bigrams(text: str) -> set[str]:
    """只取中文字符 bigram：数字、拉丁字母、标点会把不同主题误连在一起。

    带缓存：`learn_concepts` 要对全语料每条算一次。返回的是**缓存对象本身**，
    调用方只读不改（现有调用方都做集合运算，会得到新集合）。
    """
    return _cjk_bigrams_cached(text or "")


@functools.lru_cache(maxsize=8192)
def _cjk_bigrams_cached(text: str) -> set[str]:
    chars = [c for c in text if "\u4e00" <= c <= "\u9fff"]
    return {a + b for a, b in zip(chars, chars[1:])}


@functools.lru_cache(maxsize=16384)
def _bucket_of(term: str, char_dim: int) -> int:
    """词项 → 哈希桶。

    单独缓存这一步，因为它按**词**重复而不是按文本：同一批记忆里「部署」会出现
    几十次，每次 rebuild 都要重新 blake2b 一遍。缓存后命中只要一次查表。

    不用内置 hash()：它对 str 有进程级随机化，跨进程不稳定，
    而索引必须在「保存 → 重载」后给出同一批向量。
    """
    digest = int(hashlib.blake2b(term.encode("utf-8"), digest_size=4).hexdigest(), 16)
    return digest % char_dim


class ConceptEncoder:
    """把文本投影到「概念 + 字符」联合空间；词典可整体替换或注入自定义 encoder。"""

    def __init__(
        self,
        concepts: dict[str, tuple[str, ...]] | None = None,
        char_dim: int = CHAR_DIM,
        concept_weight: float = CONCEPT_W,
        char_weight: float = CHAR_W,
        ambiguous: dict[str, tuple[str, ...]] | None = None,
        ambiguity_floor: float = AMBIGUITY_FLOOR,
    ) -> None:
        self.concepts = {k: tuple(v) for k, v in (concepts or DEFAULT_CONCEPTS).items()}
        self.ambiguous = {
            k: tuple(v) for k, v in (AMBIGUOUS_TERMS if ambiguous is None else ambiguous).items()
            if any(c in self.concepts for c in v)
        }
        self.concept_keys = sorted(self.concepts)
        self.char_dim = char_dim
        self.concept_weight = concept_weight
        self.char_weight = char_weight
        self.ambiguity_floor = ambiguity_floor
        self.dim = len(self.concept_keys) + char_dim
        self._index_of = {key: i for i, key in enumerate(self.concept_keys)}
        self._lexicon: dict[str, list[int]] = {}
        for key in self.concept_keys:
            for term in self.concepts[key]:
                self._lexicon.setdefault(term, []).append(self._index_of[key])
        self._ambiguous: dict[str, list[int]] = {
            term: [self._index_of[c] for c in keys if c in self._index_of]
            for term, keys in self.ambiguous.items()
        }
        self._ambiguous = {t: idx for t, idx in self._ambiguous.items() if idx}

    # ---- 上下文裁决 ----

    def _shares(self, scores: list[float], indices: list[int]) -> list[float]:
        """按上下文份额分配多义词：有无歧义上下文就跟上下文，没有就均分。"""
        floor = max(1e-9, float(self.ambiguity_floor))
        raw = [max(0.0, scores[i]) + floor for i in indices]
        total = sum(raw)
        if total <= 0:
            return [1.0 / len(indices)] * len(indices)
        return [value / total for value in raw]

    def project(self, text: str) -> tuple[list[float], dict[str, list[float]]]:
        """把文本拆成概念分量 + 多义词裁决账目。encode 与 explain 共用同一条路径。

        不再顺带返回「中文 bigram 个数」：那个值三个调用方全部丢弃，
        却要在每次编码时构造一个约 24 元素的集合 —— 纯浪费（约 4µs/条）。
        """
        source = (text or "").lower()
        scores = [0.0] * len(self.concept_keys)
        for term, indices in self._lexicon.items():
            hits = source.count(term)
            if not hits:
                continue
            weight = self.concept_weight * (1.0 + math.log(hits))
            for index in indices:
                scores[index] += weight
        resolved: dict[str, list[float]] = {}
        for term, indices in self._ambiguous.items():
            hits = source.count(term)
            if not hits:
                continue
            weight = self.concept_weight * AMBIGUOUS_W * (1.0 + math.log(hits))
            shares = self._shares(scores, indices)
            for index, share in zip(indices, shares):
                scores[index] += weight * share
            resolved[term] = [
                round(share, 4) for share in shares
            ]
        return scores, resolved

    def encode(self, text: str) -> list[float]:
        """无 IDF 的稠密编码：给「只有一个查询、没有语料」的场景用（等价于全部词同权）。

        对外契约仍是 list[float]（`Encoder` 就这么定义的），但内部热路径走
        `encode_with` 直接产出 SparseVector，不再铺一遍 263 维数组再归一化。
        """
        return self.encode_with(None, text).to_dense()

    def encode_with(self, idf: "IdfModel | None", text: str) -> SparseVector:
        """词法编码：概念分量 + IDF 加权的词项分量，直接产出稀疏向量。

        为什么必须带 IDF：字符维原本是「每对字符等权加一」，于是
        「输出」这种满语料都是的词与「凯利」权重相同 —— 余弦被高频虚词主导，
        「构建机输出乱码」与「输出用表格」就会被判成同一件事。
        IDF 把「到处都有」压下去，把「只有这里才有」抬上来。

        为什么直接产稀疏：先铺稠密再过滤，等于把 263 维里的 245 个零算两遍
        （铺一遍、归一化时再扫一遍）。这里只在累加器里留非零项，归一化也只扫非零项。
        """
        scores, _resolved = self.project(text)
        base = len(self.concept_keys)
        char_dim = self.char_dim
        char_weight = self.char_weight
        acc: dict[int, float] = {
            index: float(score) for index, score in enumerate(scores) if score
        }
        # 空 IDF（还没语料）不能当「所有词权重 0」用 —— 那会把词项维整片抹掉，
        # 只剩概念维，两条共享概念的记忆余弦会变成 1.0。
        usable = idf if (idf is not None and idf.n > 0) else None
        for term, count in cached_tokens(text).items():
            # 内联 tf_weight 与否定词查表：这两个调用占了词项循环一半的开销，
            # 而它们都只有一行算术。保持语义完全一致（1+log tf × idf × boost）。
            multiplier = usable.weight(term) if usable is not None else 1.0
            if not multiplier:
                continue  # 语料里没有这个词：文档侧该维也是 0，加了只会抬高自身范数
            weight = (1.0 + math.log(count)) * multiplier
            if term in NEGATIONS:
                weight *= NEGATION_BOOST
            # 仍用哈希桶（维度固定、可序列化），但桶内权重带 IDF —— 碰撞不再等权叠加
            key = base + _bucket_of(term, char_dim)
            acc[key] = acc.get(key, 0.0) + char_weight * weight
        norm = math.sqrt(math.fsum(v * v for v in acc.values()))
        if not norm:
            return SparseVector.zero(self.dim)
        inv = 1.0 / norm
        unit = {i: v * inv for i, v in acc.items() if v}
        idx = tuple(sorted(unit))
        return SparseVector(idx, tuple(unit[i] for i in idx), self.dim, _map=unit)

    def __call__(self, text: str) -> list[float]:
        return self.encode(text)

    # ---- 可解释 ----

    def explain(self, text: str) -> dict[str, Any]:
        """这条文本凭什么被归到这些概念？多义词被分到了谁头上？"""
        scores, resolved = self.project(text)
        names = {
            self.concept_keys[i]: round(score, 4)
            for i, score in enumerate(scores)
            if score > 0
        }
        ambiguous = {}
        for term, shares in resolved.items():
            indices = self._ambiguous[term]
            ambiguous[term] = {
                self.concept_keys[i]: share for i, share in zip(indices, shares)
            }
        return {"concepts": names, "ambiguous": ambiguous}

    # ---- 从语料长词典 ----

    @staticmethod
    def learn_concepts(
        texts: Iterable[str],
        seeds: dict[str, tuple[str, ...]],
        min_df_ratio: float = 0.12,
        max_df_ratio: float = 0.60,
        min_lift: float = 1.6,
        min_purity: float = 0.75,
        max_terms: int = 3,
    ) -> dict[str, tuple[str, ...]]:
        """从语料里学出种子之外的概念词。

        判据（四条同时成立，缺一条就会学出噪声）：
          1. 出现频率落在 [min_df_ratio, max_df_ratio]，且至少出现在 2 篇里：
             太少是偶然（「点措」这种切词残渣），太多是「今天」这种泛用词；
          2. 与**同一概念的 ≥2 个不同种子词**同篇出现 —— 单一锚点太容易巧合；
          3. 提升度 (P(概念|词) / P(概念)) ≥ min_lift —— 只学到确实指这个词的概念；
          4. 纯净度 ≥ min_purity —— 这个词出现的篇目大多指向同一个概念。
             没有第 4 条，「记录」会同时挂到复盘与输出上，把所有主题连成一坨。

        这个函数是纯函数：同一批文本（与顺序无关）永远长出同一份词典。
        """
        docs = [t for t in texts if t]
        n = len(docs)
        if n < 3:
            return {}
        seed_sets = {k: tuple(v) for k, v in seeds.items()}
        known = {term for terms in seed_sets.values() for term in terms} | set(AMBIGUOUS_TERMS)
        df: Counter[str] = Counter()
        co: dict[str, Counter[str]] = defaultdict(Counter)
        anchors: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
        doc_hits: Counter[str] = Counter()

        for text in docs:
            terms = cjk_bigrams(text) - known
            present = {k: {s for s in seed_sets[k] if s in text} for k in seed_sets}
            for key, hits in present.items():
                if hits:
                    doc_hits[key] += 1
            for term in terms:
                df[term] += 1
                for key, hits in present.items():
                    if hits:
                        co[term][key] += 1
                        anchors[term][key] |= hits

        learned: dict[str, list[tuple[float, str]]] = defaultdict(list)
        for term, count in df.items():
            share = count / n
            if count < 2 or share < min_df_ratio or share > max_df_ratio:
                continue
            total_joint = sum(co[term].values())
            if total_joint <= 0:
                continue
            best: tuple[str, float] | None = None
            for key, joint in co[term].items():
                if len(anchors[term][key]) < 2:
                    continue
                if joint / total_joint < min_purity:
                    continue
                base = max(doc_hits[key], 1) / n
                lift = (joint / count) / base
                if lift < min_lift:
                    continue
                if best is None or lift > best[1]:
                    best = (key, lift)
            if best is not None:
                learned[best[0]].append((best[1], term))

        out: dict[str, tuple[str, ...]] = {}
        for key, items in learned.items():
            items.sort(key=lambda pair: (-pair[0], pair[1]))
            out[key] = tuple(term for _lift, term in items[:max_terms])
        return out


class SemanticIndex:
    """只对图中真实存在的记忆建索引；脏向量、幽灵条目一律拒绝。

    语料变了（新增/删除/改写记忆）就重新学一次词典并重建向量；
    因此索引状态是「图内容的纯函数」，跨进程恢复后与原先逐条一致。
    """

    def __init__(
        self, encoder: Encoder | None = None, config: Any = None, embedder: Embedder | None = None
    ) -> None:
        self.config = config
        self._custom = encoder is not None
        self.encoder: Encoder = encoder or ConceptEncoder()
        # 稠密通道：不配就是 None，行为与 0.9.x 逐位一致（默认不改变任何既有结果）。
        self.embedder: Embedder | None = (
            embedder if embedder is not None else (make_embedder(config) if config else None)
        )
        self._dense: DenseIndex | None = None
        if self.embedder is not None:
            self._dense = DenseIndex(getattr(self.embedder, "dim", 0) or 0)
        self._hybrid = float(getattr(config, "hybrid_weight", 0.0)) if config else 0.0
        self.dense_failures = 0
        self._seed_concepts: dict[str, tuple[str, ...]] = (
            dict(self.encoder.concepts) if isinstance(self.encoder, ConceptEncoder) else {}
        )
        self._learn = (not self._custom) and isinstance(self.encoder, ConceptEncoder)
        self.learned: dict[str, tuple[str, ...]] = {}
        self._vectors: dict[str, SparseVector] = {}
        # —— 倒排：分量号 → {记忆名} ——
        #
        # 一条记忆平均只占 18 个非零分量，查询也一样。两个稀疏向量的点积只可能
        # 在共享分量上非零 —— 换句话说，**没共享任何分量的记忆，分数必然是 0**。
        # 以前却要对全库每一条跑一遍点积才得出这个 0：10k 条时 10ms，100k 时 100ms，
        # 全是白烧。倒排让「候选」从「全库」缩到「至少共享一个分量的那些」（实测 25~40%）。
        #
        # 为什么用 dict 而不是 set 存桶：删除要 O(1)（遗忘是高频操作），
        # `dict[str, None]` 每项比 set 省三成内存，合并时 `set().update(dict)` 走 C 层。
        self._postings: dict[int, dict[str, None]] = {}
        self._post: dict[str, tuple[int, ...]] = {}
        self._dim: int | None = getattr(self.encoder, "dim", None)
        # 脏标记代替「每次查询都哈希全语料」：写入/遗忘即脏，重建一次后转干净。
        self._dirty = True
        # IDF 由语料现算（不落盘）：快照只存文本，权重是文本的函数，
        # 换一批记忆权重自然跟着变，不会留下陈旧统计。
        self._idf = IdfModel()
        # 编码失败的节点：记下来别反复重试（见 _fill_missing）
        self._failed: set[str] = set()

    def __len__(self) -> int:
        return len(self._vectors)

    # ---- 内部 ----

    def _validate(self, vec: Any) -> SparseVector:
        """把编码结果转成稀疏向量，并挡住坏数据。

        三段检查合并成一趟：`math.fsum` 在 C 层扫一遍，同时解决
        「含非数值（TypeError）」「含 NaN（total != total）」「含 Inf（inf 或 ValueError）」。
        原来是逐元素 isinstance + isfinite + float() 跑四遍，实测 27µs ——
        与整个编码（30µs）相当。那是纯浪费：263 个元素里有 245 个是零，
        却要逐个承认「它是零」。

        仍然拒绝维度不符与 NaN/Inf：索引一旦被污染，后面所有余弦都不可信。
        """
        if isinstance(vec, SparseVector):
            if self._dim is None:
                self._dim = vec.dim
            if vec.dim != self._dim:
                raise ValueError(f"语义向量维度异常: {vec.dim}，应为 {self._dim}")
            return vec
        if not isinstance(vec, (list, tuple)):
            raise ValueError("编码器必须返回数值向量")
        if self._dim is None:
            self._dim = len(vec)
        if not vec or len(vec) != self._dim:
            raise ValueError(f"语义向量维度异常: {len(vec)}，应为 {self._dim}")
        if self._custom and any(isinstance(v, bool) for v in vec):
            raise ValueError("语义向量含布尔值")
        try:
            total = math.fsum(vec)
        except TypeError:
            raise ValueError("语义向量含非数值分量") from None
        except (ValueError, OverflowError):
            raise ValueError("语义向量含 NaN/Inf，拒绝污染索引") from None
        if total != total or total in (_INF, _NEG_INF):
            raise ValueError(f"语义向量含 NaN/Inf，拒绝污染索引: {total!r}")
        return SparseVector.from_dense(list(vec), self._dim)

    def _learn_lexicon(self, texts: list[str]) -> None:
        if not self._learn:
            return
        cfg = self.config
        min_df = getattr(cfg, "semantic_learn_min_df_ratio", 0.12)
        max_df = getattr(cfg, "semantic_learn_max_df_ratio", 0.60)
        min_lift = getattr(cfg, "semantic_learn_min_lift", 1.6)
        min_purity = getattr(cfg, "semantic_learn_min_purity", 0.75)
        extra = ConceptEncoder.learn_concepts(
            texts,
            self._seed_concepts,
            min_df_ratio=min_df,
            max_df_ratio=max_df,
            min_lift=min_lift,
            min_purity=min_purity,
        )
        self.learned = extra
        merged: dict[str, tuple[str, ...]] = {}
        for key in sorted(set(self._seed_concepts) | set(extra)):
            terms = list(self._seed_concepts.get(key, ()))
            for term in extra.get(key, ()):
                if term not in terms:
                    terms.append(term)
            merged[key] = tuple(terms)
        encoder = self.encoder
        if isinstance(encoder, ConceptEncoder) and merged != encoder.concepts:
            self.encoder = ConceptEncoder(
                concepts=merged,
                char_dim=encoder.char_dim,
                concept_weight=encoder.concept_weight,
                char_weight=encoder.char_weight,
                ambiguous=encoder.ambiguous,
                ambiguity_floor=getattr(
                    cfg, "semantic_ambiguity_floor", encoder.ambiguity_floor
                ),
            )
            self._dim = self.encoder.dim

    # ---- 索引维护 ----

    def _doc(self, node: MemoryNode) -> str:
        """索引用的文本：正文 + 标签。

        标签是人挑的词，通常就是用户提问时会用的词（正文「不吃香菜与内脏」，
        标签「忌口」）。只索引正文时，「我有什么忌口」的得分是 **0.000** ——
        不是排得靠后，而是根本没进候选集。这条是在六领域标注集上量出来的。
        """
        tags = getattr(node, "tags", ()) or ()
        if not tags or not getattr(self.config, "index_tags", True):
            return node.content
        return node.content + " " + " ".join(str(t) for t in tags)

    def _encode(self, text: str) -> SparseVector:
        """统一入口：支持 IDF 的编码器走 encode_with，其余走 encode。"""
        encoder = self.encoder
        if hasattr(encoder, "encode_with"):
            return self._validate(encoder.encode_with(self._idf, text))
        return self._validate(encoder.encode(text))

    def _embed(self, text: str) -> list[float] | None:
        """稠密编码失败不炸主流程：降级为「这一条没有稠密分量」，并记账。

        一个远程嵌入服务抖一下，不该让整个检索不可用 —— 但也不能静默：
        `dense_failures` 与 `explain()` 会把它报出来。
        """
        if self.embedder is None:
            return None
        try:
            vec = list(self.embedder.embed(text))
        except (EmbeddingError, OSError, ValueError):
            self.dense_failures += 1
            return None
        if self._dense is not None:
            if not self._dense.dim:
                self._dense.dim = len(vec)
            if len(vec) != self._dense.dim:
                self.dense_failures += 1
                return None
        return vec

    def _index_dense(self, name: str, text: str) -> None:
        if self._dense is None:
            return
        vec = self._embed(text)
        if vec is None:
            return
        try:
            self._dense.add(name, vec)
        except ValueError:
            self.dense_failures += 1

    def _put(self, name: str, vec: SparseVector) -> None:
        """写入一条向量，同时维护倒排。

        所有写入口都必须走这里 —— 绕过它就会出现「向量在、候选里没有」的幽灵：
        查得到却召回不到，而且不会报错。这类 bug 最难查，因为它表现为「有时候不准」。
        """
        old = self._vectors.get(name)
        if old is not None:
            if old.idx == vec.idx:
                # 分量完全相同（只改了权重）：倒排不动，省一次全桶删除 + 重建
                self._vectors[name] = vec
                return
            self._drop(name)
        self._vectors[name] = vec
        if vec.idx:
            self._post[name] = vec.idx
            for index in vec.idx:
                bucket = self._postings.get(index)
                if bucket is None:
                    bucket = self._postings[index] = {}
                bucket[name] = None

    def _drop(self, name: str) -> None:
        """删一条向量与它在倒排里的全部痕迹；桶空了就删桶（否则 100k 轮换后桶数只增不减）。"""
        for index in self._post.pop(name, ()):
            bucket = self._postings.get(index)
            if bucket is not None:
                bucket.pop(name, None)
                if not bucket:
                    del self._postings[index]
        self._vectors.pop(name, None)

    def _candidates(self, q: SparseVector) -> Iterable[str]:
        """至少与查询共享一个非零分量的记忆名。

        候选外点积严格为 0，所以这里剪掉它们不改变任何分数，只是不再空转。
        倒排还没建（或全空语料）时退回全库 —— 正确性优先于快。
        """
        if not self._postings:
            return self._vectors.keys()
        seen: set[str] = set()
        for index in q.idx:
            bucket = self._postings.get(index)
            if bucket:
                seen.update(bucket)
        return seen

    def upsert(self, node: MemoryNode) -> None:
        # 这里用的是「上一次重建时的 IDF」；随后置脏，下次检索会用全语料重算，
        # 所以短暂的不一致不会留在最终索引里。
        self._put(node.name, self._encode(self._doc(node)))
        self._index_dense(node.name, node.content)
        self._dirty = True  # 语料变了，概念词典可能要重学

    def forget(self, name: str) -> None:
        self._failed.discard(name)
        if self._vectors.get(name) is not None:
            self._drop(name)
            self._dirty = True
        if self._dense is not None:
            self._dense.remove(name)

    def sparse(self, name: str) -> SparseVector | None:
        """热路径取用：点积、聚类、去重都走这个，避免每条向量摊开成 263 维数组。"""
        return self._vectors.get(name)

    def vector(self, name: str) -> list[float] | None:
        """稠密视图（兼容与诊断用）：给测试、自定义 encoder 与外部调用方。

        内部热路径**不要**用它 —— 每次调用都会新分配一个 263 元素数组。
        """
        vec = self._vectors.get(name)
        return None if vec is None else vec.to_dense()

    def rebuild(self, graph: MemoryGraph) -> None:
        docs = [self._doc(n) for n in graph.all_nodes()]
        self._learn_lexicon(docs)
        # IDF 必须先于任何编码算完：同一批向量必须共享同一把尺子，否则余弦没有意义
        self._idf = IdfModel(docs)
        self._vectors.clear()
        self._postings.clear()
        self._post.clear()
        self._failed.clear()
        if self._dense is not None:
            self._dense.clear()
        for node in graph.all_nodes():
            text = self._doc(node)
            self._put(node.name, self._encode(text))
            self._index_dense(node.name, text)
        self._dirty = False

    def _fill_missing(self, graph: MemoryGraph) -> int:
        """只补「图里有、索引里没有」的向量，不重算已有向量。

        存在的理由是一个真实缺陷：沉淀每新建一条结论节点，图的节点数就 +1，
        而 sync() 把「数量不一致」当成「必须全量重建」——一次 run_cycle 里有 8 个簇，
        就是 8 次全量重建（200 条记忆 = 1636 次编码，164ms 里 140ms 花在这）。
        补缺失是 O(k)，重建是 O(n)。

        代价必须说清：新节点用的是**当前这把 IDF 尺**，语料统计没有跟着更新。
        所以补完之后索引与「按最终语料重建」会有极小差异；
        调用方若要求严格一致（例如沉淀刚造出新内容），应在批次结束时置脏让下次重建。
        """
        live = {n.name for n in graph.all_nodes()}
        filled = 0
        for name in [k for k in self._vectors if k not in live]:
            self._drop(name)                  # 幽灵向量：图里已经没有了
            if self._dense is not None:
                self._dense.remove(name)
        for node in graph.all_nodes():
            if node.name in self._vectors or node.name in self._failed:
                continue
            try:
                self._put(node.name, self._encode(node.content))
                self._index_dense(node.name, node.content)
                filled += 1
            except ValueError:
                # 坏内容不进索引，但不阻断其他节点。
                # 记进 _failed 是必须的：否则每次 sync 都会重试同一条必然会失败的编码，
                # 让「补缺失」从 O(k) 退化成 O(n) —— 一条坏数据就能拖慢所有查询。
                self._failed.add(node.name)
        return filled

    def sync(self, graph: MemoryGraph) -> None:
        """检索前对齐。

        分两种情况，代价差一个量级：
          · 语料内容变了（dirty）→ 必须全量重建：IDF 是所有向量共用的尺子，
            尺子变了，每条向量都得重算；
          · 只是多了几条节点（沉淀新建的结论）→ 只补缺失，别重算已有的。

        以前把两者混成一件事，后果是「沉一个簇就重建一整遍」。
        """
        if self._dirty:
            self.rebuild(graph)
        elif len(self._vectors) != len(graph):
            self._fill_missing(graph)

    def mark_dirty(self) -> None:
        """显式置脏：图里出现了新内容（例如沉淀产出的结论），需要下次全量对齐。"""
        self._dirty = True

    def ghosts(self, graph: MemoryGraph) -> list[str]:
        """索引里存在、图里已删除的名字 —— 幽灵记忆检测。"""
        return sorted(set(self._vectors) - set(graph.nodes()))

    def encode_query(self, text: str) -> SparseVector:
        """对外暴露「同尺编码」：任何要跟库内向量比余弦的地方都必须走这里。

        直接用 encoder.encode() 会拿到「无 IDF 权重」的向量，与库内向量不在同一空间，
        余弦算出来的数字没有意义 —— 去重因此会静默失效（踩过）。
        """
        return self._encode(text)

    def scores(self, query: str, min_score: float = 0.0) -> dict[str, float]:
        lex = self.lexical_scores(query, 0.0)
        if self._dense is None or self._hybrid <= 0 or self.embedder is None:
            return {k: v for k, v in lex.items() if v >= min_score}
        q = self._embed(query)
        if q is None:
            return {k: v for k, v in lex.items() if v >= min_score}
        dense = self._dense.scores(q, 0.0)
        w = self._hybrid
        out: dict[str, float] = {}
        for name in lex.keys() | dense.keys():
            score = (1.0 - w) * lex.get(name, 0.0) + w * dense.get(name, 0.0)
            if score < 0.0:
                score = 0.0
            elif score > 1.0:
                score = 1.0
            if score >= min_score:
                out[name] = round(score, 6)
        return out

    def lexical_scores(self, query: str, min_score: float = 0.0) -> dict[str, float]:
        """纯词法通道（0.9.x 的行为）：调参与对照都靠它。

        只走**倒排候选**：与查询没有任何共享分量的记忆，点积恒为 0，
        以前要跑一遍才算得出这个 0。现在它们直接不进结果。

        这带来一个语义上的收紧，值得写清楚：**分数为 0 的条目不再出现在返回里**。
        以前 `min_score=0` 会把全库都交出去（绝大多数是 0 分），调用方得自己过滤；
        现在返回的就是「至少沾一个词」的那些。对排序、top-k、弃答判定都没有影响 ——
        0 分本来就排在最后，且 `max()` 在空集上仍是 0.0。
        """
        q = self._encode(query)
        if not q:
            return {}
        # 稀疏点积不必再包一层 cosine()：两边都已经 L2 归一化，点积就是余弦；
        # 只需按 cosine 的口径夹到 [0, 1]。
        out: dict[str, float] = {}
        for name in self._candidates(q):
            vec = self._vectors.get(name)
            if vec is None:  # 候选与向量表在极端时序下可能差一拍，宁可跳过
                continue
            score = q.dot(vec)
            if score > 1.0:
                score = 1.0
            elif score < 0.0:
                score = 0.0
            if score >= min_score and score > 0.0:
                out[name] = round(score, 6)
        return out

    def dense_scores(self, query: str, min_score: float = 0.0) -> dict[str, float]:
        """纯稠密通道：用于评估「换一条通道到底值不值」。"""
        if self._dense is None:
            return {}
        q = self._embed(query)
        if q is None:
            return {}
        return self._dense.scores(q, min_score)

    def pair_score(self, left: str, right: str) -> float:
        """两段任意文本的相似度（不要求它们已在索引里）。

        去重、沉淀候选、评测都要「比较两段话」，而不是「拿一段话查库」。
        两条通道的融合方式必须与 `scores()` 一致，否则评估出来的阈值
        跟线上行为对不上 —— 那种评估比没有更糟。
        """
        lex = max(0.0, min(1.0, self._encode(left).dot(self._encode(right))))
        if self._dense is None or self._hybrid <= 0:
            return lex
        va, vb = self._embed(left), self._embed(right)
        if va is None or vb is None:
            return lex
        dense = max(0.0, min(1.0, math.fsum(a * b for a, b in zip(va, vb))))
        return (1.0 - self._hybrid) * lex + self._hybrid * dense

    def scores_dense(self, query: str, min_score: float = 0.0) -> dict[str, float]:
        """同上的稠密实现，只作对照用（测试里拿它验证稀疏分支没算错）。"""
        q = self._encode(query)
        if not q:
            return {}
        dense = q.to_dense()
        return {
            name: round(score, 6)
            for name, vec in self._vectors.items()
            if (score := cosine(dense, vec)) >= min_score
        }

    def search(
        self,
        query: str,
        top_k: int = 8,
        min_score: float = 0.0,
        valid_names: Iterable[str] | None = None,
    ) -> list[tuple[str, float]]:
        allowed = set(valid_names) if valid_names is not None else None
        ranked = [
            (name, score)
            for name, score in self.scores(query, min_score).items()
            if allowed is None or name in allowed
        ]
        ranked.sort(key=lambda pair: (-pair[1], pair[0]))
        return ranked[:top_k]

    def explain(self, text: str) -> dict[str, Any]:
        encoder = self.encoder
        if not isinstance(encoder, ConceptEncoder):
            detail: dict[str, Any] = {"concepts": {}, "ambiguous": {}, "learned": {}}
        else:
            detail = encoder.explain(text)
            detail["learned"] = {k: list(v) for k, v in self.learned.items()}
        # 通道状态必须能查到：否则「为什么这条没召回」会变成猜谜
        detail["channels"] = {
            "lexical": True,
            "dense": self._dense is not None,
            "embedder": getattr(self.embedder, "name", "") if self.embedder else "",
            "hybrid_weight": self._hybrid,
            "dense_vectors": len(self._dense) if self._dense is not None else 0,
            "dense_failures": self.dense_failures,
        }
        return detail
