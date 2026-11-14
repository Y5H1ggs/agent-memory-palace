"""embedding.py - 嵌入通道：固定维度稠密向量，可换成本地模型或远程 API。

先说清楚它**不是**什么：内置的 `HashingEmbedder` 不是神经语言模型。
它是「哈希技巧 + 字符 n-gram + 有符号投影」得到的固定维度向量
（feature hashing / random indexing 这一类），优点是零依赖、确定性、
对未登录词有天然的平滑能力；缺点是它仍然读字面，不会因为「读懂了意思」而把
「止盈」与「落袋为安」放到一起 —— 那需要真正的预训练模型。

所以这一层设计成可替换的：

    ""        → 不启用（默认，行为与 0.9.x 完全一致）
    "hashing" → 内置 HashingEmbedder，512 维，零依赖
    "http"    → 任何 OpenAI 兼容的 /v1/embeddings（本地 ollama、vLLM、BGE 服务都算）

换成真模型的那一刻，检索、聚类、去重、弃答**全部**跟着换 —— 因为它们读的是
同一个 `SemanticIndex.encoder`，而不是各自实现一套「语义」。

诚实的一句话：这个库以前不是向量检索的替代品，因为它的「语义」是手工词典加 bigram；
现在它有一条真正的稠密通道，但**默认仍然不启用** —— 打开它的前提是你接受
「多一个维度空间、多一份缓存、可能多一次网络调用」，并且愿意用 `eval` 量一下值不值。
"""
from __future__ import annotations

import array
import hashlib
import json
import math
import os
import urllib.error
import urllib.request
from typing import Any, Iterable, Protocol, Sequence, runtime_checkable

from .core import atomic_write_text
from .lexicon import cached_tokens


class EmbeddingError(RuntimeError):
    pass


@runtime_checkable
class Embedder(Protocol):
    """任何满足这三个属性的对象都可当嵌入器（不需要继承）。"""

    name: str
    dim: int

    def embed(self, text: str) -> Sequence[float]: ...


def _l2(vec: list[float]) -> list[float]:
    norm = math.sqrt(math.fsum(v * v for v in vec))
    if norm <= 0:
        return vec
    return [v / norm for v in vec]


class HashingEmbedder:
    """有符号特征哈希：词项 → 固定维度的一个分量（正负由另一路哈希决定）。

    为什么要「有符号」：全正的哈希会让任意两条文本的余弦都偏高（所有分量同号相加），
    512 维下无关文本也能到 0.3，弃答线直接失效。正负抵消之后，
    无关文本的期望内积是 0，余弦分布才回到「该像的像、不该像的不像」。

    为什么要字符 n-gram：中文双字 bigram 已经由 `lexicon.tokens` 给出，
    再加 3-gram 是为了让「资金接力」与「接力资金」这类顺序变化仍能命中一部分。
    TF 用 1+log(tf) 饱和：出现十次不等于重要十倍。
    """

    def __init__(self, dim: int = 512, ngram: int = 3, seed: int = 0x5EED) -> None:
        self.dim = int(dim)
        self.ngram = int(ngram)
        self.seed = int(seed)
        self.name = f"hashing-{self.dim}"

    # 哈希必须稳定：Python 内置 hash() 带随机盐，跨进程会得到不同向量，
    # 那等于每次重启都换一个向量空间 —— 检索结果会「今天和昨天不一样」。
    def _index(self, term: str) -> tuple[int, float]:
        digest = hashlib.blake2b(
            term.encode("utf-8"), digest_size=8, key=self.seed.to_bytes(8, "little")
        ).digest()
        raw = int.from_bytes(digest, "little")
        sign = 1.0 if (raw >> 63) & 1 else -1.0
        return raw % self.dim, sign

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        counts = cached_tokens(text)
        for term, count in counts.items():
            idx, sign = self._index(term)
            vec[idx] += sign * (1.0 + math.log(count))
            if self.ngram and len(term) > 2:
                # 三字滑窗：只对足够长的词项做，短词加了反而是噪声
                for i in range(len(term) - self.ngram + 1):
                    gram = term[i : i + self.ngram]
                    gi, gs = self._index("#" + gram)
                    vec[gi] += gs * 0.5
        return _l2(vec)


class HttpEmbedder:
    """OpenAI 兼容 `/v1/embeddings`。零依赖（urllib），带磁盘缓存与超时。

    缓存是必需的，不是优化：同一段文本在一次 rebuild 里会被编码多次
    （upsert / 聚类 / 查询），每次都发一次网络请求既慢又贵。
    缓存键是 `模型名 + 文本 sha256`，换模型自动失效。
    """

    def __init__(
        self,
        endpoint: str,
        model: str,
        api_key: str = "",
        *,
        dim: int = 0,
        timeout: float = 20.0,
        cache_path: str = "",
        batch: int = 16,
        normalize: bool = True,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.batch = max(1, int(batch))
        self.normalize = normalize
        self.cache_path = cache_path
        self.name = f"http:{model}"
        self.dim = int(dim)
        self._cache: dict[str, list[float]] = {}
        self._dirty = False
        self.failures = 0
        if cache_path and os.path.exists(cache_path):
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._cache = {k: list(v) for k, v in data.items() if isinstance(v, list)}
                if not self.dim and self._cache:
                    self.dim = len(next(iter(self._cache.values())))
            except (OSError, ValueError):
                self._cache = {}

    def _key(self, text: str) -> str:
        return hashlib.sha256(f"{self.model}\x00{text}".encode("utf-8")).hexdigest()

    def embed(self, text: str) -> list[float]:
        batch = self.embed_many([text])
        if not batch:
            raise EmbeddingError("嵌入服务没有返回向量")
        return batch[0]

    def embed_many(self, texts: Iterable[str]) -> list[list[float]]:
        items = list(texts)
        missing = [t for t in items if self._key(t) not in self._cache]
        for start in range(0, len(missing), self.batch):
            self._request(missing[start : start + self.batch])
        out = []
        for text in items:
            vec = self._cache.get(self._key(text))
            if vec is None:
                raise EmbeddingError(f"向量缺失（服务端少返回）: {text[:32]!r}")
            out.append(vec)
        if self._dirty:
            self.flush()
        return out

    def _request(self, chunk: list[str]) -> None:
        if not chunk:
            return
        payload = json.dumps({"model": self.model, "input": chunk}).encode("utf-8")
        req = urllib.request.Request(
            self.endpoint,
            data=payload,
            headers={
                "Content-Type": "application/json",
                **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}),
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            self.failures += 1
            raise EmbeddingError(f"嵌入请求失败: {exc}") from None
        data = body.get("data") or []
        if len(data) != len(chunk):
            raise EmbeddingError(f"返回数量不符: {len(data)} != {len(chunk)}")
        order = sorted(data, key=lambda d: int(d.get("index", 0)))
        for text, item in zip(chunk, order):
            vec = [float(v) for v in item.get("embedding") or []]
            if not vec:
                raise EmbeddingError("空向量")
            if not self.dim:
                self.dim = len(vec)
            if len(vec) != self.dim:
                raise EmbeddingError(f"维度不一致: {len(vec)} != {self.dim}（换模型了？）")
            self._cache[self._key(text)] = _l2(vec) if self.normalize else vec
            self._dirty = True

    def flush(self) -> None:
        if not (self.cache_path and self._dirty):
            return
        self._dirty = False
        try:
            atomic_write_text(self.cache_path, json.dumps(self._cache, ensure_ascii=False))
        except OSError:
            pass

    def __len__(self) -> int:
        return len(self._cache)


class CallableEmbedder:
    """把一个普通的 `text -> list[float]` 函数包成 Embedder。

    给测试和「用户自己接本地模型」用：只要能出向量，形状对不对由 `SemanticIndex._validate` 管。
    """

    def __init__(self, fn, dim: int, name: str = "callable") -> None:
        self._fn = fn
        self.dim = int(dim)
        self.name = name

    def embed(self, text: str) -> list[float]:
        vec = self._fn(text)
        if len(vec) != self.dim:
            raise EmbeddingError(f"维度不符: {len(vec)} != {self.dim}")
        return list(vec)


def make_embedder(config: Any) -> Embedder | None:
    """按配置造嵌入器。造不出来就返回 None（并让上层如实报告，而不是假装有语义）。"""
    provider = getattr(config, "embedding_provider", "") or ""
    if not provider:
        return None
    if provider == "hashing":
        return HashingEmbedder(dim=getattr(config, "embedding_dim", 512))
    if provider == "http":
        key = os.environ.get(getattr(config, "embedding_key_env", ""), "") or ""
        cache = getattr(config, "embedding_cache", "") or ""
        return HttpEmbedder(
            getattr(config, "embedding_endpoint", ""),
            getattr(config, "embedding_model", "") or "default",
            key,
            dim=getattr(config, "embedding_dim", 0) if getattr(config, "embedding_dim", 512) else 0,
            cache_path=cache,
        )
    return None


class DenseIndex:
    """名字 → 稠密向量的索引，余弦用全表扫（朴素但诚实）。

    为什么不上 ANN：本库的目标规模是 1e4 量级（一条记忆一行，交给 SQLite），
    在这个量级全表扫 512 维约 3ms，比维护一个 ANN 结构更省心；
    真到 1e5 再换，届时接口不变（`scores()` 换实现即可）。
    """

    def __init__(self, dim: int = 0) -> None:
        self.dim = int(dim)
        self._vecs: dict[str, array.array] = {}

    def __len__(self) -> int:
        return len(self._vecs)

    def clear(self) -> None:
        self._vecs.clear()

    def add(self, name: str, vec: Sequence[float]) -> None:
        if not self.dim:
            self.dim = len(vec)
        if len(vec) != self.dim:
            raise ValueError(f"稠密向量维度不一致: {len(vec)} != {self.dim}")
        self._vecs[name] = array.array("f", vec)

    def remove(self, name: str) -> None:
        self._vecs.pop(name, None)

    def get(self, name: str) -> array.array | None:
        return self._vecs.get(name)

    def scores(self, query: Sequence[float], min_score: float = 0.0) -> dict[str, float]:
        if not self._vecs or len(query) != self.dim:
            return {}
        q = array.array("f", query)
        # 两边都已 L2 归一化，点积即余弦
        out: dict[str, float] = {}
        for name, vec in self._vecs.items():
            score = math.fsum(a * b for a, b in zip(q, vec))
            if score >= min_score:
                out[name] = round(max(0.0, min(1.0, score)), 6)
        return out
