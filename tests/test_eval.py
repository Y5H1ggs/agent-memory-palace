"""test_eval.py - 评测集自身的可靠性。

评测集最容易出的错不是「指标算错」，而是「标注错了」：
gold 里写了不存在的名字，召回永远是 0；或者一堆 unanswerable 其实是能答的。
所以这里先审数据，再审指标，最后审「标定出来的阈值是否真的比默认值好」。
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from memory_palace import evaluation as ev  # noqa: E402


def test_datasets_are_well_formed() -> None:
    for name, ds in ev.DATASETS.items():
        names = [m[0] for m in ds.memories]
        assert len(names) == len(set(names)), f"{name} 有重名记忆"
        known = set(names)
        for q in ds.queries:
            assert q.kind in ("exact", "paraphrase", "unanswerable"), (name, q)
            for gold in q.gold:
                assert gold in known, f"{name} 的查询「{q.text}」标注了不存在的 {gold}"
            if q.kind == "unanswerable":
                assert not q.gold, f"{name} 的不可答查询不该有 gold: {q.text}"
            else:
                assert q.gold, f"{name} 的可答查询必须有 gold: {q.text}"
        for cluster in ds.clusters:
            for member in cluster:
                assert member in known, f"{name} 的簇里有不存在的 {member}"
        for a, b, _same in ds.dedupe_pairs:
            assert a and b and a != b, f"{name} 的去重对非法"


def test_both_domains_present() -> None:
    """两个领域缺一不可：单领域数据集会让任何阈值都显得很好。"""
    assert {"zh-finance", "zh-tech"} <= set(ev.DATASETS)
    for ds in ev.DATASETS.values():
        assert len(ds.queries) >= 15
        assert any(q.kind == "unanswerable" for q in ds.queries)


def test_metrics_math() -> None:
    assert ev.recall_at_k(["a", "b"], {"a"}, 2) == 1.0
    assert ev.recall_at_k(["b", "c"], {"a"}, 2) == 0.0
    assert ev.mrr(["b", "a"], {"a"}) == 0.5
    assert ev.mrr(["a"], {"a"}) == 1.0
    # 首位命中时 nDCG 必须是 1.0
    assert abs(ev.ndcg_at_k(["a", "b"], {"a"}, 2) - 1.0) < 1e-9
    # 第二位命中时低于 1.0，且高于第三位
    second = ev.ndcg_at_k(["x", "a"], {"a"}, 3)
    third = ev.ndcg_at_k(["x", "y", "a"], {"a"}, 3)
    assert 0 < third < second < 1.0


def test_suite_shape() -> None:
    report = ev.run_suite(k=5)
    assert report["k"] == 5 and report["channel"] == "lexical"
    assert len(report["datasets"]) == 2
    entry = report["datasets"][0]
    for key in ("retrieval", "abstain", "dedupe", "consolidation", "consolidation_swept"):
        assert key in entry, key
    assert entry["retrieval"]["overall"]["n"] > 10
    text = ev.format_report(report)
    assert "recall" in text and "弃答" in text


def test_retrieval_beats_random() -> None:
    """词法检索必须显著好于随机：这是一个下限检查，防「评测根本没在测」。"""
    report = ev.run_retrieval(ev.FINANCE, k=5, channel="lexical")
    overall = report["overall"]
    # 40 条记忆、随机 5 条命中 1 条 gold 的概率约 12%
    assert overall["recall@k"] >= 0.4, overall
    assert overall["mrr"] >= 0.35, overall


def test_calibration_finds_usable_thresholds() -> None:
    result = ev.calibrate(["zh-finance", "zh-tech"])
    dedupe = result["dedupe"]
    abstain = result["abstain"]
    assert 0.7 <= dedupe["recommended"] <= 1.0, dedupe
    assert 0.1 <= abstain["recommended"] <= 0.6, abstain
    assert dedupe["best"]["f1"] >= 0.6, dedupe
    assert abstain["best"]["f1"] >= 0.6, abstain
    # 平台区间必须包含推荐值
    low, high = dedupe["plateau"]
    assert low <= dedupe["recommended"] <= high


def test_calibration_is_not_a_single_peak() -> None:
    """只有尖峰的阈值不能用：旁边两步就掉一半的东西，换个语料必翻车。"""
    result = ev.calibrate(["zh-finance"])
    curve = result["curves"]["dedupe"]
    best = max(row["f1"] for row in curve)
    near = [row for row in curve if abs(row["threshold"] - result["dedupe"]["recommended"]) <= 0.1]
    assert near and min(row["f1"] for row in near) >= best - 0.25, near


def test_dedupe_threshold_sweep_monotonic_input() -> None:
    """阈值越高，召回不增、精确不减 —— 基本单调性，防扫描逻辑写反。"""
    low = ev.run_dedupe(ev.FINANCE, threshold=0.75)
    high = ev.run_dedupe(ev.FINANCE, threshold=0.98)
    assert high["precision"] >= low["precision"]
    assert high["recall"] <= low["recall"]


def test_abstain_separates_answerable_from_not() -> None:
    """弃答必须在两个数据集上都比瞎猜强（F1 > 0.5），否则这条线没有意义。"""
    for ds in ev.DATASETS.values():
        report = ev.run_abstain(ds, threshold=0.18)
        assert report["abstain_f1"] >= 0.8, (ds.name, report)


def test_scale_benchmark_runs() -> None:
    rows = ev.scale_benchmark((200, 400))
    assert [row["nodes"] for row in rows] == [200, 400]
    for row in rows:
        assert row["rebuild_ms"] > 0
        assert row["p95_query_ms"] >= row["p50_query_ms"] >= 0


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  [PASS] {name}")
    print("\n评测集自测通过 ✅")
