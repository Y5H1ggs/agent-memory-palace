"""lexicon.py - 词法层：分词、归一、IDF 权重、否定词。

这一层是整个语义栈的地基。地基歪了，上面所有东西（检索、沉淀、弃答线、跨项目合并）
都在将就：同一句话换个说法召回不到，无关的两句话因为共享「输出」二字被聚成一类。
那不是聚类算法的问题，是**词法层没给词分轻重**。

四件事，每件都有一个具体的坑：

1. **归一**：全角/半角、大小写、`UTF-8` 与 `utf8`。
   技术记忆里这类差异极多，不归一等于同一个词被当成两个。

2. **分词**：
   · 中文取双字 bigram —— 不引入分词器是刻意的（零依赖），
     双字足以表达「部署」「迁移」「凯利」，代价是「工程师」会被拆成「工程」「程师」；
   · 英文/数字/代码按词边界抽，并把复合词拆开（`env.sh` → `env.sh` + `env` + `sh`），
     因为技术文本里标识符才是强特征；
   · 单字默认丢弃（噪声大），**但否定词保留**。

3. **IDF**：高频词降权。没有这一步，`输出`（满语料都是）和 `凯利`（只出现两次）
   在余弦里权重相同 —— 于是「构建机输出乱码」与「输出用表格」被判成同一件事。
   IDF 从当前语料现算（不落盘）：快照里只存文本，权重是文本的函数，
   换一批记忆权重自然跟着变，不会留下陈旧统计。

4. **否定词**：`不追高` 与 `追高` 意思相反，但 bigram 差一个字、余弦却很高。
   所以单字里唯一被保留的就是否定词，并给它们额外权重。
   这是词法层最容易被忽略、后果最严重的一处：漏掉否定 = 把结论说反。
"""
from __future__ import annotations

import functools
import math
import re
import unicodedata
from collections import Counter
from typing import Iterable

# 中文连续段：只在这些段里取 bigram
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
# 拉丁/数字标识符：允许 . _ / # + - 连接（env.sh、v1.2、feat/x、utf-8）
_WORD = re.compile(r"[a-z0-9]+(?:[._/#+-][a-z0-9]+)*")
_SEP = re.compile(r"[._/#+-]")

# 否定词：唯一保留的单字。漏掉它们会把「不追高」和「追高」判成近义。
NEGATIONS = frozenset("不没无非别勿禁未莫")
NEGATION_BOOST = 2.0

# 单字停用表：只用于**单字**，不影响 bigram。
# 「上/下/中」这类字在 bigram 里是有信息的（上线、下调、中间），所以只挡它们的单字形态。
STOP_CHARS = frozenset("的了是在和与就都也还把被给对为这那有会要着过吗呢吧啊哦嗯")


def normalize(text: str) -> str:
    """NFKC 归一（全角→半角）+ 小写 + 去掉多余空白。

    NFKC 顺手把「（」「）」「－」这些全角标点折成半角，省掉一堆特判。
    """
    raw = unicodedata.normalize("NFKC", text or "")
    return " ".join(raw.lower().split())


@functools.lru_cache(maxsize=8192)
def _tokens_cached(text: str) -> Counter[str]:
    return tokens(text)


def tokens(text: str) -> Counter[str]:
    """文本 → 词频表。

    返回 Counter 而不是 set：词频要参与 TF 饱和（1+log tf），
    出现三次和出现一次不该等价。
    """
    source = normalize(text)
    out: Counter[str] = Counter()

    for run in _CJK_RUN.findall(source):
        # 双字 bigram：中文的主力特征
        for a, b in zip(run, run[1:]):
            out[a + b] += 1
        # 单字只留否定词（其余单字噪声太大）
        for ch in run:
            if ch in NEGATIONS:
                out[ch] += 1

    for word in _WORD.findall(source):
        out[word] += 1
        parts = [p for p in _SEP.split(word) if len(p) >= 2]
        if len(parts) > 1:
            # 复合标识符同时保留整体与部件：env.sh 既算一个词，也让 env / sh 各自可命中
            for part in parts:
                out[part] += 1

    for key in [k for k in out if len(k) == 1 and k in STOP_CHARS]:
        del out[key]

    return out


def cached_tokens(text: str) -> Counter[str]:
    """带缓存的版本：一次 rebuild 里同一段文本要分词两遍（IDF 统计 + 向量编码）。

    返回的是缓存对象本身，调用方只读不改 —— 改动它会污染缓存。
    """
    return _tokens_cached(text)


def explain(text: str) -> list[tuple[str, int]]:
    """给「为什么命中」用：按词频降序列出词项。"""
    return [(term, count) for term, count in tokens(text).most_common()]


class IdfModel:
    """从语料现算的 IDF。

    `weight = log((N+1)/(df+1)) + 1`：平滑后恒正，未登录词拿到最高权重。
    未登录词高权重是对的 —— 罕见词本身就是最强特征；
    但它也意味着「query 里有一个语料中从未出现的词」时单项就能拉满余弦，
    所以检索侧仍要靠 `semantic_min_score` 与相对门槛兜底（见 cli 的弃答判据）。
    """

    __slots__ = ("n", "df", "_cache")

    def __init__(self, docs: Iterable[str] = ()) -> None:
        df: Counter[str] = Counter()
        n = 0
        for doc in docs:
            n += 1
            df.update(tokens(doc).keys())  # 用 keys()：一篇文档里重复出现只算一次 df
        self.n = n
        self.df = df
        self._cache: dict[str, float] = {}

    def weight(self, term: str) -> float:
        """语料里没出现过的词，权重为 0 —— 它区分不了任何两条文档。

        这条不是优化，是修正。直觉上「罕见的词最有信息量」，于是把未登录词的 IDF
        设成最大值；但中文按 bigram 切，跨词边界会切出大量从未出现过的碎片
        （`怎么处理` → `么处`、`码怎`）。给它们最高权重，结果是**查询向量被噪声拉长**，
        真正的共同词反而被稀释，余弦整体变低。

        0 才是对的：文档侧这些维度本来就是 0，查询侧给多少权重都不会命中，
        只会抬高自己的 L2 范数。
        """
        cached = self._cache.get(term)
        if cached is not None:
            return cached
        df = self.df.get(term)
        if df is None:
            return 0.0
        value = math.log((self.n + 1) / (df + 1)) + 1.0
        self._cache[term] = value
        return value

    def idf_of(self, text: str, top: int = 5) -> list[tuple[str, float]]:
        """最「有区分度」的几个词 —— 面板与 explain 用它回答「凭什么」。"""
        # 只列语料里真实存在的词：未登录词权重 0，列出来只会误导
        scored = [
            (term, weight)
            for term in cached_tokens(text)
            if (weight := self.weight(term)) > 0
        ]
        scored.sort(key=lambda pair: (-pair[1], pair[0]))
        return scored[:top]

    def to_dict(self, limit: int = 40) -> dict[str, float]:
        """只给诊断用（不进快照）：词表本身可能很大。"""
        top = sorted(self.df.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
        return {term: round(self.weight(term), 3) for term, _ in top}


def tf_weight(count: int, idf: float, boost: float = 1.0) -> float:
    """词频饱和 × IDF。出现三次不等于权重三倍（1+log tf）。"""
    return (1.0 + math.log(count)) * idf * boost
