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

import hashlib
import math
from collections import Counter, defaultdict
from typing import Any, Callable, Iterable

from .core import MemoryGraph, MemoryNode

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

CHAR_DIM = 64
CONCEPT_W = 1.0
CHAR_W = 0.22
# 多义词的权重低于无歧义词：它本来就不该单独决定一条记忆属于什么主题。
AMBIGUOUS_W = 0.8
AMBIGUITY_FLOOR = 0.15

Encoder = Callable[[str], list[float]]


def _l2(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0.0:
        return vec
    return [v / norm for v in vec]


def cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        raise ValueError("语义向量维度不一致")
    return max(0.0, min(1.0, sum(x * y for x, y in zip(a, b))))


def cjk_bigrams(text: str) -> set[str]:
    """只取中文字符 bigram：数字、拉丁字母、标点会把不同主题误连在一起。"""
    chars = [c for c in (text or "") if "\u4e00" <= c <= "\u9fff"]
    return {a + b for a, b in zip(chars, chars[1:])}


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

    def project(self, text: str) -> tuple[list[float], dict[str, list[float]], int]:
        """把文本拆成概念分量 + 多义词裁决账目。encode 与 explain 共用同一条路径。"""
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
        return scores, resolved, len(cjk_bigrams(source))

    def encode(self, text: str) -> list[float]:
        scores, _resolved, _chars = self.project(text)
        vec = [float(s) for s in scores] + [0.0] * self.char_dim
        source = (text or "").lower()
        chars = [c for c in source if not c.isspace()]
        for a, b in zip(chars, chars[1:]):
            bucket = int(
                hashlib.blake2b((a + b).encode("utf-8"), digest_size=4).hexdigest(), 16
            )
            vec[len(self.concept_keys) + bucket % self.char_dim] += self.char_weight
        return _l2(vec)

    def __call__(self, text: str) -> list[float]:
        return self.encode(text)

    # ---- 可解释 ----

    def explain(self, text: str) -> dict[str, Any]:
        """这条文本凭什么被归到这些概念？多义词被分到了谁头上？"""
        scores, resolved, _chars = self.project(text)
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

    def __init__(self, encoder: Encoder | None = None, config: Any = None) -> None:
        self.config = config
        self._custom = encoder is not None
        self.encoder: Encoder = encoder or ConceptEncoder()
        self._seed_concepts: dict[str, tuple[str, ...]] = (
            dict(self.encoder.concepts) if isinstance(self.encoder, ConceptEncoder) else {}
        )
        self._learn = (not self._custom) and isinstance(self.encoder, ConceptEncoder)
        self.learned: dict[str, tuple[str, ...]] = {}
        self._vectors: dict[str, list[float]] = {}
        self._dim: int | None = getattr(self.encoder, "dim", None)
        # 脏标记代替「每次查询都哈希全语料」：写入/遗忘即脏，重建一次后转干净。
        self._dirty = True

    def __len__(self) -> int:
        return len(self._vectors)

    # ---- 内部 ----

    @staticmethod
    def _graph_signature(graph: MemoryGraph) -> bytes:
        digest = hashlib.blake2b(digest_size=16)
        for name, content in sorted((n.name, n.content) for n in graph.all_nodes()):
            digest.update(name.encode("utf-8"))
            digest.update(b"\x00")
            digest.update(content.encode("utf-8"))
            digest.update(b"\x01")
        return digest.digest()

    def _validate(self, vec: Any) -> list[float]:
        if not isinstance(vec, (list, tuple)):
            raise ValueError("编码器必须返回数值向量")
        values = list(vec)
        if self._dim is None:
            self._dim = len(values)
        if not values or len(values) != self._dim:
            raise ValueError(f"语义向量维度异常: {len(values)}，应为 {self._dim}")
        for value in values:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"语义向量含非数值分量: {value!r}")
            if not math.isfinite(float(value)):
                raise ValueError(f"语义向量含 NaN/Inf，拒绝污染索引: {value!r}")
        return [float(v) for v in values]

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

    def upsert(self, node: MemoryNode) -> None:
        self._vectors[node.name] = self._validate(self.encoder.encode(node.content))
        self._dirty = True  # 语料变了，概念词典可能要重学

    def forget(self, name: str) -> None:
        if self._vectors.pop(name, None) is not None:
            self._dirty = True

    def vector(self, name: str) -> list[float] | None:
        """给沉淀聚类等外部模块取用；未建索引的节点返回 None 而不是零向量。"""
        return self._vectors.get(name)

    def rebuild(self, graph: MemoryGraph) -> None:
        self._learn_lexicon([n.content for n in graph.all_nodes()])
        self._vectors.clear()
        for node in graph.all_nodes():
            self._vectors[node.name] = self._validate(self.encoder.encode(node.content))
        self._dirty = False

    def sync(self, graph: MemoryGraph) -> None:
        """检索前对齐：脏了（或向量数与图不符）才重建。

        以前是每次查询都算一遍全语料哈希 —— 1000 条记忆每次查询要扫 30KB 文本。
        现在只读一个布尔量，重建由写入方负责置脏。
        """
        if self._dirty or len(self._vectors) != len(graph):
            self.rebuild(graph)

    def ghosts(self, graph: MemoryGraph) -> list[str]:
        """索引里存在、图里已删除的名字 —— 幽灵记忆检测。"""
        return sorted(set(self._vectors) - set(graph.nodes()))

    def scores(self, query: str, min_score: float = 0.0) -> dict[str, float]:
        q = self._validate(self.encoder.encode(query))
        if not any(q):
            return {}
        return {
            name: round(score, 6)
            for name, vec in self._vectors.items()
            if (score := cosine(q, vec)) >= min_score
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
            return {"concepts": {}, "ambiguous": {}, "learned": {}}
        detail = encoder.explain(text)
        detail["learned"] = {k: list(v) for k, v in self.learned.items()}
        return detail
