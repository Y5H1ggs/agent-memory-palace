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


def test_domain_coverage() -> None:
    """领域数本身就是指标：两三个领域上标定出来的阈值，换个语料必翻车。

    这条测试守着「不要再退回两领域」—— 0.20 的弃答线正是在两领域上看着很好，
    扩到六领域后才发现它误弃 4 条、一个也没多抓到。
    """
    expected = {"zh-finance", "zh-tech", "zh-medical", "zh-legal", "zh-support", "zh-personal"}
    assert expected <= set(ev.DATASETS), sorted(set(ev.DATASETS))
    for ds in ev.DATASETS.values():
        assert len(ds.queries) >= 10, (ds.name, len(ds.queries))
        assert sum(1 for q in ds.queries if q.kind == "unanswerable") >= 2, ds.name
        assert ds.memories and ds.clusters, ds.name


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
    assert len(report["datasets"]) == len(ev.DATASETS)
    # 汇总必须存在：单领域数字容易被「挑好看的领域」误导
    assert report["summary"]["domains"] == len(ev.DATASETS)
    assert report["summary"]["queries"] >= 60
    entry = report["datasets"][0]
    for key in ("retrieval", "abstain", "dedupe", "consolidation", "consolidation_swept"):
        assert key in entry, key
    assert entry["retrieval"]["overall"]["n"] > 10
    text = ev.format_report(report)
    assert "recall" in text and "弃答" in text
    assert "跨 6 领域" in text


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
    """弃答必须在每个领域上都比瞎猜强，且用**线上默认阈值**。

    之前这条用的是写死的 0.18，等于「测另一个系统」。现在直接读配置默认值：
    默认值改了而这条测试没跟着红，才是真正危险的情况。
    """
    from memory_palace.core import PalaceConfig

    threshold = PalaceConfig().abstain_semantic_floor
    for ds in ev.DATASETS.values():
        report = ev.run_abstain(ds)  # 不传阈值 = 用配置默认值
        assert report["threshold"] == threshold
        assert report["abstain_f1"] >= 0.7, (ds.name, report)


def test_tag_only_queries_are_indexed() -> None:
    """只出现在标签里的词也必须能被检索到。

    「我有什么忌口」在只索引正文时得分是 0.000 —— 正文写的是「不吃香菜与内脏」。
    这不是排序问题，是根本没进候选集；标签是人挑的词，往往就是提问的措辞。
    """
    from memory_palace.core import PalaceConfig

    index = ev.build_index(ev.PERSONAL, config=PalaceConfig(index_tags=True))
    scored = index.scores("我有什么忌口")
    assert scored.get("me:food", 0) > 0, scored
    # 关掉开关时要能复现旧行为，否则这个选项是死的
    plain = ev.build_index(ev.PERSONAL, config=PalaceConfig(index_tags=False))
    assert plain.scores("我有什么忌口").get("me:food", 0) == 0


def test_hash_collisions_do_not_fake_similarity() -> None:
    """毫无词项重叠的查询不该拿到高分。

    256 个哈希桶时，「量子计算的退相干时间是多少」对一条讲破产概率的金融记忆
    打出 0.455 —— 两者一个共享词都没有，那纯粹是桶碰撞。
    这类假分数会同时毒化检索与弃答：它让「没记过」看起来像「记过」。
    """
    from memory_palace.lexicon import cached_tokens

    index = ev.build_index(ev.FINANCE)
    query = "量子计算的退相干时间是多少"
    scored = index.scores(query)
    for name, score in sorted(scored.items(), key=lambda kv: -kv[1])[:3]:
        node = ev.FINANCE.graph().get(name)
        shared = set(cached_tokens(query)) & set(
            cached_tokens(node.content + " " + " ".join(node.tags))
        )
        if not shared:
            assert score < 0.3, f"{name} 无共享词项却得 {score:.3f}（哈希碰撞）"


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
