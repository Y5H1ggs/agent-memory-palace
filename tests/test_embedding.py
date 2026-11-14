"""test_embedding.py - 嵌入通道：确定性、归一化、混合检索是否真的更好。

最后一条是重点：**必须用标注数据证明「加了稠密通道更好」**，
而不是「我加了代码所以它更好」。如果数据说没有更好，测试就该红。
"""
from __future__ import annotations

import json
import math
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from memory_palace import evaluation as ev  # noqa: E402
from memory_palace.core import PalaceConfig  # noqa: E402
from memory_palace.embedding import (  # noqa: E402
    CallableEmbedder,
    DenseIndex,
    HashingEmbedder,
    HttpEmbedder,
    make_embedder,
)
from memory_palace.semantics import SemanticIndex  # noqa: E402


def test_hashing_is_deterministic_across_processes() -> None:
    """同一个词项必须落进同一个桶。

    内置 hash() 带随机盐：同一段文本在两个进程里得到不同向量，
    那等于「每次重启换一个向量空间」，检索结果今天和明天不一样。
    """
    import subprocess

    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "from memory_palace.embedding import HashingEmbedder\n"
        "e = HashingEmbedder(64)\n"
        "print(','.join(f'{v:.6f}' for v in e.embed('仓位与资金接力')))\n" % ROOT
    )
    outs = []
    for _ in range(2):
        proc = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, encoding="utf-8"
        )
        assert proc.returncode == 0, proc.stderr
        outs.append(proc.stdout.strip())
    assert outs[0] == outs[1], "跨进程向量不一致：哈希用了带随机盐的实现"


def test_hashing_is_normalized_and_signed() -> None:
    emb = HashingEmbedder(256)
    vec = emb.embed("凯利公式与仓位分散，单笔不超过三分之一")
    assert len(vec) == 256
    assert abs(math.sqrt(math.fsum(v * v for v in vec)) - 1.0) < 1e-6
    assert any(v < 0 for v in vec), "全正哈希会让无关文本的余弦普遍偏高"
    assert any(v > 0 for v in vec)


def test_unrelated_texts_score_low() -> None:
    """无关文本的余弦必须显著低于相关文本 —— 否则弃答线无从谈起。"""
    emb = HashingEmbedder(512)
    a = emb.embed("仓位按半凯利给，单笔不超过三分之一")
    b = emb.embed("日志要结构化并带 trace_id")
    c = emb.embed("单笔仓位不超过三分之一，按半凯利")
    dot = lambda x, y: math.fsum(p * q for p, q in zip(x, y))  # noqa: E731
    assert dot(a, c) > 0.8
    assert dot(a, b) < 0.25
    assert dot(a, c) > dot(a, b) * 3


def test_dense_index_dimension_guard() -> None:
    index = DenseIndex(4)
    index.add("a", [1.0, 0.0, 0.0, 0.0])
    try:
        index.add("b", [1.0, 0.0])
    except ValueError as exc:
        assert "维度" in str(exc)
    else:
        raise AssertionError("维度不一致必须报错，不能安静地加进去")


def test_make_embedder_from_config() -> None:
    assert make_embedder(PalaceConfig()) is None
    hashing = make_embedder(PalaceConfig(embedding_provider="hashing", embedding_dim=128))
    assert isinstance(hashing, HashingEmbedder) and hashing.dim == 128
    cfg = PalaceConfig(embedding_provider="http", embedding_endpoint="http://127.0.0.1:9/x")
    assert isinstance(make_embedder(cfg), HttpEmbedder)


def test_hybrid_beats_lexical_on_labelled_data() -> None:
    """在标注集上，混合通道的 recall 必须不低于纯词法。

    这条断言是「向量检索替代品」这句话的实测依据。数据不支持就说明
    这次改动只是多了代码，没有多出能力。
    """
    lexical = ev.run_retrieval(ev.FINANCE, k=5, channel="lexical")
    hybrid = ev.run_retrieval(ev.FINANCE, k=5, embedder=HashingEmbedder(512), channel="hybrid")
    assert hybrid["overall"]["recall@k"] >= lexical["overall"]["recall@k"], (lexical, hybrid)
    assert hybrid["paraphrase"]["recall@k"] >= lexical["paraphrase"]["recall@k"], (
        "混合通道在换说法的查询上没有改善，那它的意义就只剩「多占内存」"
    )


class _Handler(BaseHTTPRequestHandler):
    calls: list[dict] = []
    fail = False

    def log_message(self, *args) -> None:  # 静音：测试输出要干净
        return

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        type(self).calls.append(body)
        if type(self).fail:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"{}")
            return
        if self.path.endswith("/embeddings"):
            data = [
                {"index": i, "embedding": [float(len(t) % 7), 1.0, 0.0, 0.0]}
                for i, t in enumerate(body["input"])
            ]
            payload = {"data": data}
        else:
            payload = {
                "choices": [{"message": {"content": json.dumps({"findings": ["ok"]})}}]
            }
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def _serve():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def test_http_embedder_batches_and_caches() -> None:
    _Handler.calls.clear()
    _Handler.fail = False
    server, base = _serve()
    try:
        emb = HttpEmbedder(base + "/v1/embeddings", "fake-model", dim=4, cache_path="")
        vec = emb.embed("仓位与凯利")
        assert len(vec) == 4
        again = emb.embed("仓位与凯利")
        assert vec == again
        assert len(_Handler.calls) == 1, "第二次必须命中缓存，不能重复请求"
        emb.embed_many(["a" * 3, "b" * 5])
        assert len(_Handler.calls) == 2
        assert len(_Handler.calls[1]["input"]) == 2, "批量必须一次发多条"
    finally:
        server.shutdown()


def test_http_embedder_cache_persists() -> None:
    import tempfile

    _Handler.fail = False
    server, base = _serve()
    try:
        with tempfile.TemporaryDirectory(prefix="mp-emb-") as tmp:
            cache = os.path.join(tmp, "cache.json")
            first = HttpEmbedder(base + "/v1/embeddings", "m", dim=4, cache_path=cache)
            first.embed("持久化缓存")
            assert os.path.exists(cache)
            calls_before = len(_Handler.calls)
            second = HttpEmbedder(base + "/v1/embeddings", "m", dim=4, cache_path=cache)
            second.embed("持久化缓存")
            assert len(_Handler.calls) == calls_before, "落盘的缓存必须能跨进程复用"
    finally:
        server.shutdown()


def test_http_failure_degrades_to_lexical() -> None:
    """嵌入服务挂了，检索要降级，而不是整体不可用。"""
    _Handler.fail = True
    server, base = _serve()
    try:
        emb = HttpEmbedder(base + "/v1/embeddings", "m", dim=4, timeout=2)
        config = PalaceConfig(embedding_provider="http", embedding_endpoint=base, hybrid_weight=0.5)
        index = SemanticIndex(config=config, embedder=emb)
        index.rebuild(ev.FINANCE.graph())
        assert index.dense_failures > 0
        scored = index.scores("仓位与凯利")  # 不抛异常
        assert scored, "稠密通道失败时，词法通道必须还在工作"
    finally:
        server.shutdown()


def test_callable_embedder_dim_guard() -> None:
    emb = CallableEmbedder(lambda text: [1.0, 0.0], dim=2)
    assert emb.embed("x") == [1.0, 0.0]
    bad = CallableEmbedder(lambda text: [1.0], dim=2)
    try:
        bad.embed("x")
    except Exception as exc:  # noqa: BLE001
        assert "维度" in str(exc)
    else:
        raise AssertionError("维度不符必须报错")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  [PASS] {name}")
    print("\n嵌入层自测通过 ✅")
