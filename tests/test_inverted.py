"""test_inverted.py - 倒排索引与剪枝的契约。

这三条最容易在重构时被悄悄破坏，而且**破坏了也不会报错**：

1. 倒排候选必须覆盖所有「分数非零」的记忆。漏一条 = 明明存在却召不回，
   表现为「有时候不准」，最难查。
2. 删除必须同时清倒排。只删向量表会留下幽灵候选，`_candidates` 里取到
   `None` 被静默跳过 —— 看起来正常，实际上每次查询都在为死数据付遍历成本。
3. 两阶段早停必须真的等价于全量打分。早停条件写松一点就是**漏召回**，
   写紧一点就白算；它靠的是 `total ≥ base` 这个上界，必须用断言钉住。
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from memory_palace import MemoryPalace, MutableClock  # noqa: E402

TEXTS = [
    ("a", "半凯利仓位管理，单笔不超过总资金三分之一", ("仓位",)),
    ("b", "仓位按凯利公式给，最多三只分散", ("仓位",)),
    ("c", "板块资金接力看主线与连板基因", ("板块",)),
    ("d", "尾盘竞价强度决定次日溢价", ("盘口",)),
    ("e", "输出优先表格化，措辞精简", ("偏好",)),
    ("f", "红烧肉要炖多久才软烂", ("做饭",)),
]

QUERIES = ["仓位怎么分配", "板块资金接力看什么", "输出格式", "怎么炖肉", "量子退相干"]


def _palace() -> MemoryPalace:
    palace = MemoryPalace(clock=MutableClock())
    for name, text, tags in TEXTS:
        palace.remember(name, text, tags=tags)
    palace.semantics.sync(palace.graph)
    return palace


def _bruteforce(index, query: str) -> dict[str, float]:
    """全量扫描的参考实现：不剪枝、不跳 0 分，只保留非零。"""
    q = index._encode(query)
    out: dict[str, float] = {}
    for name, vec in index._vectors.items():
        score = q.dot(vec)
        score = 1.0 if score > 1.0 else (0.0 if score < 0.0 else score)
        if score > 0.0:
            out[name] = round(score, 6)
    return out


def test_candidates_cover_every_nonzero_score():
    palace = _palace()
    index = palace.semantics
    for query in QUERIES:
        fast = index.lexical_scores(query)
        ref = _bruteforce(index, query)
        assert set(fast) == set(ref), f"{query}: 候选漏了 {set(ref) - set(fast)}"
        assert fast == ref, f"{query}: 分数不一致"


def test_zero_score_entries_are_not_returned():
    """0 分不是命中。这条以前是虚高的来源：全库都返回，靠字典序占位。"""
    index = _palace().semantics
    assert index.lexical_scores("量子退相干") == {}
    assert index.scores("量子退相干") == {}


def test_forget_clears_postings():
    palace = _palace()
    index = palace.semantics
    before = set(index._postings.keys())
    palace.drop("a", reason="test")
    palace.semantics.sync(palace.graph)
    assert "a" not in index._vectors
    assert "a" not in index._post
    for bucket in index._postings.values():
        assert "a" not in bucket, "幽灵候选：向量删了，倒排还留着"
    # 桶空了要删桶，否则长期轮换后桶数只增不减
    assert set(index._postings.keys()) <= before
    assert all(bucket for bucket in index._postings.values()), "空桶没被清掉"


def test_rebuild_matches_incremental():
    """全量重建与「先脏后重建」必须得到同一个索引 —— 含倒排。"""
    palace = _palace()
    incremental = {q: dict(palace.semantics.lexical_scores(q)) for q in QUERIES}
    palace.semantics.rebuild(palace.graph)
    rebuilt = {q: dict(palace.semantics.lexical_scores(q)) for q in QUERIES}
    assert incremental == rebuilt


def test_upsert_with_same_components_keeps_postings():
    """只改权重不改字面时，倒排不该被拆了重建（分量相同可以原地换）。"""
    palace = _palace()
    index = palace.semantics
    node = palace.graph.get("a")
    assert node is not None
    old_idx = index._vectors["a"].idx
    palace.remember("a", node.content, tags=node.tags, tier="core")
    palace.semantics.sync(palace.graph)
    assert index._vectors["a"].idx == old_idx
    for position in old_idx:
        assert "a" in index._postings[position]


def test_two_stage_pruning_equals_full_ranking():
    """两阶段早停 vs 全量打分：top-k 与分数都必须逐位相同。"""
    palace = _palace()
    index = palace.semantics
    cfg = palace._config
    gate = cfg.semantic_min_score
    for query in QUERIES:
        full = palace._score_components(query, (), palace.now())
        above = [r for r in full if r["score"] >= gate and (r["semantic"] > 0 or r["tag"] > 0)]
        for limit in (1, 2, 5):
            expected = above[:limit]
            got = palace._score_components(
                query, (), palace.now(), only_hits=True, gate=gate, limit=limit
            )
            assert [r["name"] for r in got] == [r["name"] for r in expected], query
            assert [r["score"] for r in got] == [r["score"] for r in expected], query


def test_tag_channel_still_scans_whole_graph():
    """有标签条件时必须走全图：标签命中不依赖语义，那是另一条召回通道。"""
    palace = _palace()
    hits = palace.recall(query_tags=("做饭",), limit=3)
    assert [n.name for n in hits] == ["f"]
