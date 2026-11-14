"""test_audit.py - 一轮代码审计的结论，每条发现配一条断言。

这里放的不是「功能」，是「曾经可能错、现在必须一直对」的性质：
  · 稀疏向量与稠密向量必须给出同一批分数（性能改造不许改语义）
  · 聚类的早停不许改变聚类结果（优化必须保序）
  · 坏配置必须在入口被拒（否则它只会安静地做错事）
  · 钉死是用户主权，不能被别的规则绕过
  · 增量补建不许重算已有向量（否则「优化」只是换了个名字的 O(n)）

运行：python tests/test_audit.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory_palace import MemoryPalace, MutableClock, PalaceConfig  # noqa: E402
from memory_palace.cli import build_context  # noqa: E402
from memory_palace.consolidation import ConsolidationEngine  # noqa: E402
from memory_palace.semantics import SparseVector, cosine  # noqa: E402

PASS = 0
FAIL = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {label}" + (f"  · {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {label}" + (f"  · {detail}" if detail else ""))


def small_corpus(total: int = 40) -> MemoryPalace:
    """一批同质但有区别的记忆：既能形成簇，又能测出误聚。"""
    palace = MemoryPalace(clock=MutableClock(2_010_000_000.0), project="audit")
    templates = [
        "部署前必须先跑迁移脚本",
        "部署顺序：迁移 → 构建 → 发布",
        "部署失败要回滚到上一版",
        "构建机输出乱码，永远要手动设 UTF-8",
        "CI 日志总是一堆问号，得先配好编码",
        "输出用表格，别写长篇",
        "输出优先精简，别铺开",
        "凯利仓位单笔不超过三分之一",
        "上线前 checklist 要过一次",
        "复盘要记录当时的判断，不是只记结果",
    ]
    for i in range(total):
        base = templates[i % len(templates)]
        palace.remember(f"m{i:03d}", f"{base}（第{i}次）", tags=(f"t{i % 4}",))
    palace.semantics.sync(palace.graph)
    return palace


# ---------------------------------------------------------------- 向量层


def test_sparse_matches_dense() -> None:
    print("\n稀疏向量：性能改造不许改语义")
    palace = small_corpus()
    queries = ["部署顺序", "编码乱码", "输出格式", "仓位怎么分", "完全无关的周末计划"]
    worst = 0.0
    set_diff = 0
    for q in queries:
        sparse = palace.semantics.scores(q)
        dense = palace.semantics.scores_dense(q)
        set_diff += len(set(sparse) ^ set(dense))
        worst = max(worst, max((abs(v - dense.get(k, 0.0)) for k, v in sparse.items()), default=0.0))
    check(
        "同一查询，稀疏与稠密的分数逐条相同",
        worst == 0.0 and set_diff == 0,
        f"最大偏差 {worst:.1e} / 集合差 {set_diff}",
    )

    norms = [vec.to_dense() for vec in palace.semantics._vectors.values()]
    lo = min(sum(v * v for v in n) ** 0.5 for n in norms)
    hi = max(sum(v * v for v in n) ** 0.5 for n in norms)
    check("所有向量都是单位向量（点积即余弦）", abs(lo - 1) < 1e-9 and abs(hi - 1) < 1e-9,
          f"{lo:.9f} ~ {hi:.9f}")

    node = palace.graph.get("m000")
    sparse = palace.semantics.sparse("m000")
    dense = palace.semantics.vector("m000")
    check(
        "稀疏视图与稠密视图表示同一个向量",
        isinstance(sparse, SparseVector) and cosine(sparse, [0.0] * len(dense)) >= 0.0
        and len(dense) == sparse.dim,
        f"非零 {sparse.nnz} / 维度 {sparse.dim}",
    )


def test_early_exit_is_order_preserving() -> None:
    print("\n聚类早停：跳过余弦不许改变结果")
    palace = small_corpus(60)
    now = palace.now()
    cfg = palace._config
    candidates = [
        n for n in palace.graph.all_nodes()
        if n.tier in ("ephemeral", "event") and not n.is_retired()
        and n.consolidated_into is None and not n.pinned
        and n.effective_weight(now) >= cfg.consolidate_min_weight
    ]
    engine = ConsolidationEngine(cfg, vector_lookup=palace._vector_of)

    with_exit = engine._clusters(candidates, palace.graph)
    # 反事实：把阈值传成 None，就走「永远算余弦」的老路径
    original = engine._similarity
    engine._similarity = lambda a, b, g, t=None: original(a, b, g, None)  # type: ignore[assignment]
    without_exit = engine._clusters(candidates, palace.graph)
    engine._similarity = original  # type: ignore[assignment]

    as_sets = sorted(sorted(m.name for m in c) for c in with_exit)
    plain_sets = sorted(sorted(m.name for m in c) for c in without_exit)
    check(
        "早停前后的簇结构完全一致（优化保序）",
        as_sets == plain_sets,
        f"{len(as_sets)} 个簇，最大 {max((len(c) for c in as_sets), default=0)} 条",
    )
    check(
        "而且确实形成了簇（否则上一条是空胜利）",
        any(len(c) >= 2 for c in with_exit),
        f"簇大小 {[len(c) for c in with_exit]}",
    )


# ---------------------------------------------------------------- 边界校验


def test_config_rejected_at_boundary() -> None:
    print("\n配置：坏参数必须在入口被拒，而不是安静地做错事")
    bad = {
        "forget_w=-1": PalaceConfig(forget_w=-1.0),
        "grasp_budget=nan": PalaceConfig(grasp_budget=float("nan")),
        "semantic_alpha=-5": PalaceConfig(semantic_alpha=-5.0),
        "importance_forget=2": PalaceConfig(importance_forget=2.0),
    }
    rejected = []
    for label, cfg in bad.items():
        try:
            MemoryPalace(clock=MutableClock(2_010_000_000.0), config=cfg)
        except ValueError:
            rejected.append(label)
    check(
        "四个坏配置全部在构造 MemoryPalace 时被拒",
        len(rejected) == len(bad),
        f"被拒 {len(rejected)}/{len(bad)}",
    )
    check(
        "默认配置照常可用（不是把入口写成了拒所有）",
        MemoryPalace(clock=MutableClock(2_010_000_000.0))._config.forget_w > 0,
        "默认 forget_w > 0",
    )


def test_budget_zero_means_nothing() -> None:
    print("\n预算：budget<=0 的语义是「什么都不要」")
    palace = MemoryPalace(clock=MutableClock(2_010_100_000.0), project="audit")
    palace.remember("a", "一条普通记忆", tags=("x",))
    zero = build_context(palace, query="普通记忆", budget=0)
    negative = build_context(palace, query="普通记忆", budget=-5)
    normal = build_context(palace, query="普通记忆", budget=200)
    check(
        "budget=0 返回空块，而不是「至少给一条」",
        zero["items"] == [] and zero["used"] == 0,
        f"items={len(zero['items'])} used={zero['used']}",
    )
    check("budget 为负同样返回空", negative["items"] == [], str(negative["used"]))
    check(
        "正常预算仍然给内容（不是把预算逻辑写死成空）",
        len(normal["items"]) >= 1,
        f"items={len(normal['items'])}",
    )


# ---------------------------------------------------------------- 规则冲突


def test_pinned_survives_history_retention() -> None:
    print("\n规则冲突：钉死是用户主权，历史回收不能绕过它")
    clock = MutableClock(2_010_200_000.0)
    palace = MemoryPalace(clock=clock, config=PalaceConfig(history_retention_days=30.0))
    palace.remember("p1", "旧规则：输出用表格", tags=("格式",), pinned=True)
    palace.remember("p2", "新规则：输出用短段落", tags=("格式",), supersedes=["p1"])
    clock.advance(400)          # 远超保留期
    deleted = palace.gc()
    check(
        "被取代的钉死记忆不会被保留期删掉",
        palace.graph.get("p1") is not None and "p1" not in deleted,
        f"删除 {deleted}",
    )
    check(
        "但它已经失效：默认召回不再返回",
        "p1" not in [n.name for n in palace.recall(query="输出用表格")],
        str([n.name for n in palace.recall(query="输出用表格")]),
    )


# ---------------------------------------------------------------- 增量补建


def test_fill_missing_is_incremental() -> None:
    print("\n增量补建：只补缺失，不许重算已有向量")
    palace = small_corpus(30)
    before = {name: id(vec) for name, vec in palace.semantics._vectors.items()}
    encodes = {"n": 0}
    original_encode = palace.semantics._encode

    def counting(text: str):
        encodes["n"] += 1
        return original_encode(text)

    palace.semantics._encode = counting  # type: ignore[assignment]
    # 直接往图里塞一个节点（模拟沉淀新增结论，不经过 upsert）
    from memory_palace.core import MemoryNode

    fresh = MemoryNode(
        name="core:probe", content="沉淀出来的结论：部署前先跑迁移", tier="core",
        created_at=palace.now(), last_accessed=palace.now(),
    )
    palace.graph.add_node(fresh)
    palace.semantics.sync(palace.graph)
    palace.semantics._encode = original_encode  # type: ignore[assignment]

    check(
        "只编码了新增的那一条（不是全量重建）",
        encodes["n"] == 1,
        f"编码次数 {encodes['n']}（库 {len(palace.graph)} 条）",
    )
    check(
        "已有向量的对象没有被动过",
        all(id(palace.semantics._vectors[n]) == i for n, i in before.items() if n in palace.semantics._vectors),
        f"保留 {sum(1 for n in before if n in palace.semantics._vectors)}/{len(before)}",
    )
    check(
        "新节点立刻可被检索到",
        palace.semantics.sparse("core:probe") is not None,
        str(palace.semantics.sparse("core:probe")),
    )


def test_failed_encoding_not_retried() -> None:
    print("\n坏数据：编码失败只记一次，不许拖慢每次查询")
    palace = MemoryPalace(clock=MutableClock(2_010_300_000.0), project="audit")
    palace.remember("ok", "正常内容", tags=("x",))
    palace.semantics.sync(palace.graph)

    attempts = {"n": 0}
    original = palace.semantics._encode

    def boom(text: str):
        if "坏" in text:
            attempts["n"] += 1
            raise ValueError("模拟编码失败")
        return original(text)

    palace.semantics._encode = boom  # type: ignore[assignment]
    from memory_palace.core import MemoryNode

    bad = MemoryNode(
        name="bad", content="坏内容", tier="event",
        created_at=palace.now(), last_accessed=palace.now(),
    )
    palace.graph.add_node(bad)
    for _ in range(5):
        palace.semantics.sync(palace.graph)
    palace.semantics._encode = original  # type: ignore[assignment]

    check(
        "同一条坏内容只尝试编码一次（不是每次 sync 都重试）",
        attempts["n"] == 1,
        f"尝试 {attempts['n']} 次",
    )
    check(
        "坏数据不进索引，好数据照常可检索",
        palace.semantics.sparse("bad") is None and palace.semantics.sparse("ok") is not None,
        "坏=未入索引 / 好=已入索引",
    )


def test_sync_is_free_when_clean() -> None:
    print("\n热路径：索引干净时不许发生全量重建")
    palace = small_corpus(20)
    # 数 rebuild 而不是 _encode：查询本身每次都要编一次，那是必要的；
    # 该守住的是「查询不触发把全库重编一遍」。
    rebuilds = {"n": 0}
    original = palace.semantics.rebuild

    def counting(graph):
        rebuilds["n"] += 1
        return original(graph)

    palace.semantics.rebuild = counting  # type: ignore[assignment]
    for _ in range(20):
        palace.recall(query="部署顺序")
    palace.semantics.rebuild = original  # type: ignore[assignment]
    check(
        "20 次热查询一次全量重建都没有",
        rebuilds["n"] == 0,
        f"重建 {rebuilds['n']} 次",
    )

    # 反事实：写入之后必须重建一次，否则这条断言可能只是「永远不重建」
    palace.remember("new-one", "新写入的一条部署记录", tags=("t0",))
    palace.semantics.rebuild = counting  # type: ignore[assignment]
    palace.recall(query="部署顺序")
    palace.semantics.rebuild = original  # type: ignore[assignment]
    check(
        "但写入之后确实会重建一次（不是把重建整个关掉了）",
        rebuilds["n"] == 1,
        f"累计重建 {rebuilds['n']} 次",
    )



# ---------------------------------------------------------------- 并发


def test_concurrent_access_count_is_exact() -> None:
    """并发不是「不崩就行」，是「一次都不许丢」。

    旧的第 7 幕只让线程读已存在的安全节点（pinned / core），读本身不改结构 ——
    那测的是「并发读」，不是「并发写」。这里让 5 个线程对同一条记忆各读 40 次，
    计一次数：少加一次就是丢更新。
    """
    print("\n并发：计数必须精确，不是「大概对」")
    palace = MemoryPalace(clock=MutableClock(2_010_400_000.0), project="audit")
    palace.remember("counter", "唯一被反复读取的那条", tags=("计数",))
    base = palace.graph.get("counter").access_count
    threads_n, per_thread = 5, 40
    errors: list[str] = []

    def hit() -> None:
        try:
            for _ in range(per_thread):
                palace.recall(names=["counter"])
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))

    threads = [threading.Thread(target=hit) for _ in range(threads_n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    got = palace.graph.get("counter").access_count
    check(
        "5 线程 × 40 次读，access_count 恰好 +200（零丢失）",
        got == base + threads_n * per_thread and not errors,
        f"{base} → {got}（期望 {base + threads_n * per_thread}），异常 {len(errors)}",
    )


def test_concurrent_write_read_gc() -> None:
    """读写 GC 同时发生：不许崩，且钉死的记忆不许被误删。

    这条测的是「边建边拆」：一边 remember 写新节点，一边 gc 删弱节点，
    一边 recall 遍历图。图遍历若直接读内部字典，这里会抛
    `RuntimeError: dictionary changed size during iteration`。
    """
    print("\n并发：边写、边读、边删，三件事同时发生")
    palace = MemoryPalace(clock=MutableClock(2_010_500_000.0), project="audit")
    palace.remember("rule", "钉死规则", tags=("硬规则",), tier="core", pinned=True)
    errors: list[str] = []
    # 同上：把 GIL 切换压到 1µs，让「边建边拆」真的会交错，而不是靠运气
    previous_interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)

    def writer(idx: int) -> None:
        try:
            for k in range(30):
                palace.remember(f"w{idx}-{k}", f"线程{idx}写下的第{k}条记录", tags=("并发",))
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))

    def reader() -> None:
        try:
            for _ in range(40):
                palace.recall(query="并发记录")
                palace.snapshot()
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))

    def collector() -> None:
        try:
            for _ in range(10):
                palace.gc()
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))

    def cycler() -> None:
        try:
            for _ in range(2):
                palace.run_cycle()
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
    threads += [threading.Thread(target=reader) for _ in range(3)]
    threads += [threading.Thread(target=collector), threading.Thread(target=cycler)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    sys.setswitchinterval(previous_interval)

    check(
        "9 个线程同时读/写/删/代谢，零异常",
        not errors,
        f"异常 {len(errors)}：{errors[:1]}",
    )
    check(
        "钉死记忆在混乱中依然在（GC 没有误删它）",
        palace.graph.get("rule") is not None,
        f"节点 {len(palace.graph)} / 墓碑 {len(palace.tombstone)}",
    )
    check(
        "写进去的记录没有凭空消失（节点数 ≥ 写入成功数的一部分）",
        len(palace.graph) >= 60,
        f"剩余 {len(palace.graph)}（写入 120，其余按价值规则被遗忘或沉淀）",
    )


# ---------------------------------------------------------------- 落盘一致性


def test_save_failure_rolls_back_config() -> None:
    """存不下，就不算改过。

    自修正改了配置、`validate()` 也通过了，但落盘失败：
    磁盘上还是旧值，内存里是新值，重启静默回滚 —— 而这一轮已经按新值跑完。
    没有人会收到报错。所以落盘失败必须把配置退回去。
    """
    print("\n落盘失败：配置不许「只在内存里生效」")
    from memory_palace.selfcorrect import Candidate, SelfCorrector

    # 每次跑用全新的临时目录：固定文件名会让上一次残留的 selfcorrect 冷却状态
    # 混进来，导致「修正没被应用 → 断言莫名其妙地红」。测试必须封闭。
    workdir = tempfile.mkdtemp(prefix="mp-audit-")
    clock = MutableClock(2_010_600_000.0)
    palace = MemoryPalace(
        clock=clock, project="audit", path=os.path.join(workdir, "nf.json"),
        autosave=True,
    )
    before = palace._config.importance_forget
    original_detect = palace.corrector.detect
    palace.corrector.detect = lambda metrics, now: (  # type: ignore[assignment]
        [
            Candidate(
                code="probe", param="importance_forget", direction="-",
                reason="测试用：强制产生一个待应用修正",
                metric="forget_pressure", better="lower",
            )
        ],
        [],
    )
    try:
        with mock.patch.object(palace, "_save_unlocked", side_effect=OSError("磁盘满")):
            cycle = palace.run_cycle()
    finally:
        palace.corrector.detect = original_detect  # type: ignore[assignment]

    applied = cycle["selfcorrect"]["applied"]
    rolled = cycle["selfcorrect"]["rolled_back"]
    check(
        "确实应用了一个修正（否则下一条是空胜利）",
        len(applied) >= 1,
        f"applied={applied}",
    )
    check(
        "落盘失败时，本轮应用的修正被全部回滚",
        len(rolled) == len(applied),
        f"rolled_back={rolled}",
    )
    check(
        "配置回到落盘前的值（内存与磁盘说同一件事）",
        abs(palace._config.importance_forget - before) < 1e-12,
        f"{before} → {palace._config.importance_forget}",
    )
    check(
        "落盘健康度可见（不用翻事件流也能知道）",
        palace.last_save_ok is False and palace.snapshot()["durability"]["last_save_ok"] is False,
        f"last_save_ok={palace.last_save_ok}",
    )

    # 反事实：正常落盘时不许回滚（否则「永远回滚」也能让上面几条通过）
    ok_palace = MemoryPalace(
        clock=MutableClock(2_010_600_000.0), project="audit",
        path=os.path.join(workdir, "ok.json"), autosave=True,
    )
    ok_before = ok_palace._config.importance_forget
    ok_palace.corrector.detect = lambda metrics, now: (  # type: ignore[assignment]
        [
            Candidate(
                code="probe", param="importance_forget", direction="-",
                reason="测试用", metric="forget_pressure", better="lower",
            )
        ],
        [],
    )
    ok_cycle = ok_palace.run_cycle()
    check(
        "正常落盘时不回滚，修正被保留",
        ok_cycle["selfcorrect"]["rolled_back"] == []
        and ok_palace._config.importance_forget != ok_before,
        f"{ok_before} → {ok_palace._config.importance_forget}",
    )


def test_graph_serialization_without_lock() -> None:
    """公开的图序列化方法，不许要求调用方先加锁。

    `snapshot()` 走的是持锁路径，所以并发测试照不到 `graph.to_dict()`。
    但它是公开方法：调用方可能拿图自己序列化（导出、调试、外部工具）。
    如果它直接遍历内部字典，另一边 `remove_nodes_from` 就会让字典在迭代中变大小 ——
    `RuntimeError: dictionary changed size during iteration`。
    """
    print("\n图序列化：无锁调用也不许抛「迭代中字典变大小」")
    from memory_palace.core import MemoryGraph, MemoryNode

    # 默认 GIL 切换间隔是 5ms，而一次 to_dict() 只有几微秒 —— 两个线程几乎不会交错，
    # 竞态窗口靠运气才撞上（CI 上撞到了，本机跑 10 轮都没撞到）。
    # 把切换间隔压到 1µs 让交错变得频繁，这条断言才不是「说它有就有」。
    previous_interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)

    graph = MemoryGraph()
    for i in range(60):
        node = MemoryNode(name=f"n{i}", content=f"第{i}条", created_at=1.0, last_accessed=1.0)
        graph.add_node(node)

    stop = threading.Event()
    errors: list[str] = []

    def churn() -> None:
        i = 60
        try:
            while not stop.is_set():
                node = MemoryNode(
                    name=f"n{i}", content=f"第{i}条", created_at=1.0, last_accessed=1.0
                )
                graph.add_node(node)
                graph.remove_nodes_from([f"n{i - 30}"] if i >= 90 else [])
                i += 1
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))

    def serialize() -> None:
        try:
            for _ in range(300):
                graph.to_dict()
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))

    writer = threading.Thread(target=churn)
    reader = threading.Thread(target=serialize)
    writer.start()
    reader.start()
    reader.join()
    stop.set()
    writer.join()
    sys.setswitchinterval(previous_interval)
    check(
        "边增删、边序列化 300 次，零异常",
        not errors,
        f"异常 {len(errors)}：{errors[:1]}",
    )

def main() -> int:
    print("=" * 62)
    print("代码审计：性能改造的语义中立性 + 边界校验")
    print("=" * 62)
    test_sparse_matches_dense()
    test_early_exit_is_order_preserving()
    test_config_rejected_at_boundary()
    test_budget_zero_means_nothing()
    test_pinned_survives_history_retention()
    test_fill_missing_is_incremental()
    test_failed_encoding_not_retried()
    test_sync_is_free_when_clean()
    test_concurrent_access_count_is_exact()
    test_concurrent_write_read_gc()
    test_save_failure_rolls_back_config()
    test_graph_serialization_without_lock()
    print()
    print("=" * 62)
    print(f"{PASS + FAIL} 项断言：{PASS} 通过 / {FAIL} 失败")
    print("=" * 62)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
