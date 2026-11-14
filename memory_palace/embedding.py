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


class OllamaEmbedder(HttpEmbedder):
    """本机 Ollama（`ollama serve`）。零依赖，只用标准库。

    端点分两代，都必须支持：
      · `/api/embed`（新）：入参 `{"model", "input": [..]}`，出参 `{"embeddings": [[..], ..]}`
      · `/api/embeddings`（旧）：一次一条，入参 `{"model", "prompt"}`，出参 `{"embedding": [..]}`
    只支持新的会让一堆人的旧版本直接不可用；只支持旧的则拿不到批量的好处。
    先打新的，404/405 或返回体没有 embeddings 时退回旧的，并把结果记住，后续不再试探。

    与 `HttpEmbedder` 的唯一区别是端点与返回体结构，所以缓存、超时、维度校验、
    失败计数全部继承 —— 那部分逻辑已经写过一遍，不重复。
    """

    def __init__(
        self,
        host: str = "http://127.0.0.1:11434",
        model: str = "bge-m3",
        *,
        timeout: float = 60.0,
        cache_path: str = "",
        batch: int = 16,
        dim: int = 0,
    ) -> None:
        host = (host or "http://127.0.0.1:11434").rstrip("/")
        super().__init__(
            f"{host}/api/embed",
            model,
            "",
            dim=dim,
            timeout=timeout,
            cache_path=cache_path,
            batch=batch,
        )
        self.host = host
        self.name = f"ollama:{model}"
        self.legacy = False  # 首次成功后记住走哪一代端点

    def _post(self, url: str, payload: dict) -> dict:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in (404, 405):
                raise FileNotFoundError(url) from None
            self.failures += 1
            raise EmbeddingError(f"Ollama 返回 {exc.code}: {url}") from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            self.failures += 1
            raise EmbeddingError(f"Ollama 请求失败: {exc}") from None

    def _store(self, text: str, vec: list[float]) -> None:
        if not vec:
            raise EmbeddingError("Ollama 返回空向量")
        if not self.dim:
            self.dim = len(vec)
        if len(vec) != self.dim:
            raise EmbeddingError(f"维度不一致: {len(vec)} != {self.dim}（换模型了？）")
        self._cache[self._key(text)] = _l2(vec) if self.normalize else vec
        self._dirty = True

    def _request(self, chunk: list[str]) -> None:
        if not chunk:
            return
        if not self.legacy:
            try:
                body = self._post(
                    f"{self.host}/api/embed", {"model": self.model, "input": chunk}
                )
                vectors = body.get("embeddings") or []
                if vectors and len(vectors) == len(chunk):
                    for text, vec in zip(chunk, vectors):
                        self._store(text, [float(v) for v in vec])
                    return
            except FileNotFoundError:
                self.legacy = True
        # 旧端点：一次一条
        for text in chunk:
            body = self._post(
                f"{self.host}/api/embeddings", {"model": self.model, "prompt": text}
            )
            self._store(text, [float(v) for v in body.get("embedding") or []])


class SentenceTransformerEmbedder:
    """本地 sentence-transformers 模型（BGE / m3e / gte / Qwen3-Embedding 等）。

    这是唯一「真·神经语言模型」路径，也是唯一带第三方依赖的路径 ——
    所以它在**用到时**才 import。没装就报一句能直接照做的话，
    而不是在模块导入时把整个包拖下水。

    缓存只放内存：本地推理没有网络成本，落盘反而会跟模型版本脱节
    （换 checkpoint 而缓存没换，向量会静默错配）。
    """

    def __init__(
        self,
        model: str = "BAAI/bge-small-zh-v1.5",
        *,
        device: str = "",
        batch: int = 32,
        trust_remote_code: bool = False,
    ) -> None:
        try:
            from sentence_transformers import SentenceTransformer  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover - 取决于环境
            raise EmbeddingError(
                "provider=st 需要 sentence-transformers：pip install sentence-transformers\n"
                "（不想装依赖就用 provider=http 接任意 OpenAI 兼容端点，或 provider=ollama）"
            ) from exc
        self.name = f"st:{model}"
        self.model_name = model
        self._model = SentenceTransformer(model, device=device or None, trust_remote_code=trust_remote_code)
        self._batch = max(1, int(batch))
        self.dim = int(self._model.get_sentence_embedding_dimension() or 0)
        self._cache: dict[str, list[float]] = {}

    def embed(self, text: str) -> list[float]:
        return self.embed_many([text])[0]

    def embed_many(self, texts: Iterable[str]) -> list[list[float]]:
        items = list(texts)
        missing = [t for t in items if t not in self._cache]
        for start in range(0, len(missing), self._batch):
            chunk = missing[start : start + self._batch]
            vectors = self._model.encode(
                chunk, normalize_embeddings=True, convert_to_numpy=True
            )
            for text, vec in zip(chunk, vectors):
                self._cache[text] = [float(v) for v in vec]
        return [self._cache[t] for t in items]


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


def self_check(embedder: Embedder) -> dict[str, Any]:
    """用三句话验证「这个模型到底能不能用」。

    只看维度是不够的：一个把什么都编码成同方向的模型也有 1024 维。
    这里比的是**同义句余弦应当明显高于无关句**，顺带给一条人类可读的判决。
    故意不用「专业术语对」（止盈 / 落袋为安），因为那是最理想的样例，
    理想样例通过不代表日常查询能用；用一句大白话的改写更有代表性。
    """
    related_a = "单笔仓位按半凯利给，最多不超过总资金三分之一"
    related_b = "每次买入不要超过总资金的三分之一"
    unrelated = "红烧肉要炖多久才软烂"
    texts = [related_a, related_b, unrelated]
    batch = getattr(embedder, "embed_many", None)
    vectors = batch(texts) if callable(batch) else [embedder.embed(t) for t in texts]
    def cos(x: list[float], y: list[float]) -> float:
        return round(sum(a * b for a, b in zip(x, y)), 4)
    same, diff = cos(vectors[0], vectors[1]), cos(vectors[0], vectors[2])
    name = getattr(embedder, "name", type(embedder).__name__)
    return {
        "name": name,
        "dim": len(vectors[0]),
        "related": same,
        "unrelated": diff,
        "margin": round(same - diff, 4),
        # 判决看**差距**而不是绝对余弦：同样一对句子，512 维和 2048 维的
        # 绝对余弦本来就不一样（维度越大、能量摊得越开）。用绝对值当门槛，
        # 换个维度就会误判 —— 这也是「只看维度」不靠谱的同一类错误。
        "ok": (same - diff) >= 0.2 and same >= 0.3,
    }


def make_embedder(config: Any) -> Embedder | None:
    """按配置造嵌入器。造不出来就返回 None（并让上层如实报告，而不是假装有语义）。"""
    provider = getattr(config, "embedding_provider", "") or ""
    if not provider:
        return None
    if provider == "hashing":
        return HashingEmbedder(dim=getattr(config, "embedding_dim", 512))
    if provider in ("http", "openai"):
        key = os.environ.get(getattr(config, "embedding_key_env", ""), "") or ""
        cache = getattr(config, "embedding_cache", "") or ""
        return HttpEmbedder(
            getattr(config, "embedding_endpoint", ""),
            getattr(config, "embedding_model", "") or "default",
            key,
            dim=getattr(config, "embedding_dim", 0) if getattr(config, "embedding_dim", 512) else 0,
            cache_path=cache,
        )
    if provider == "ollama":
        cache = getattr(config, "embedding_cache", "") or ""
        return OllamaEmbedder(
            getattr(config, "embedding_endpoint", "") or "http://127.0.0.1:11434",
            getattr(config, "embedding_model", "") or "bge-m3",
            cache_path=cache,
        )
    if provider in ("st", "sentence-transformers"):
        return SentenceTransformerEmbedder(
            getattr(config, "embedding_model", "") or "BAAI/bge-small-zh-v1.5"
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
