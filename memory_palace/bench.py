"""bench.py - 基准：写入、检索、注入、代谢各花多少时间与 token。

    python -m memory_palace.bench            # 默认 200 / 1000 条
    python -m memory_palace.bench 3000 --json

为什么带基准：这套东西的卖点之一是「能在 agent 每一步里被调用」。
那是**性能承诺**，不是形容 —— 没有数字的承诺，迟早会被一次卡顿打回原形。

对比的两组数是关键：**树状注入 vs 扁平注入**在同一预算下各用多少字符。
扁平注入的用量会随存量线性膨胀；树状只看枝的摘要，涨得慢得多。
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from typing import Any

from .core import MutableClock
from .palace import MemoryPalace

TOPICS = ("复盘", "仓位", "选股", "输出", "风险", "板块", "偏好", "杂记")
FILLER = (
    "今天的判断是缩量反弹不追高，等放量确认再动手",
    "半凯利仓位管理，单笔不超过总资金三分之一",
    "板块资金接力看主线与连板基因，首板后分歧票容易被抛弃",
    "输出优先表格化，措辞精简，不要长篇",
    "风险提示放在末尾，不构成投资建议",
    "复盘时先看板块横向对比，再看个股形态",
    "把教训写成一句话，别写过程",
    "记录一条观察：尾盘竞价强度决定次日溢价",
)


def make_palace(n: int, path: str | None = None) -> MemoryPalace:
    palace = MemoryPalace(path=path, clock=MutableClock())
    for i in range(n):
        topic = TOPICS[i % len(TOPICS)]
        text = f"{FILLER[i % len(FILLER)]}（#{i}）"
        palace.remember(
            f"b{i:05d}",
            text,
            tags=(topic,),
            tier="fact" if i % 7 == 0 else "event",
            pinned=(i < 3),
        )
    return palace


def _timed(fn) -> tuple[float, Any]:
    start = time.perf_counter()
    result = fn()
    return (time.perf_counter() - start) * 1000, result


def run_one(n: int) -> dict[str, Any]:
    from .cli import build_context, format_context
    from .tree import build_tree, render_tree, tree_stats

    tmp = tempfile.mkdtemp(prefix="mp-bench-")
    store = os.path.join(tmp, "store.sqlite")

    write_ms, palace = _timed(lambda: make_palace(n))
    save_ms, _ = _timed(lambda: palace.save(store))

    # 首次查询要建索引（最坏情况）
    cold_ms, _ = _timed(lambda: palace.recall(query="仓位怎么分配"))
    warm_ms, _ = _timed(lambda: palace.recall(query="仓位怎么分配"))

    ctx_ms, ctx = _timed(lambda: build_context(palace, query="仓位怎么分配", budget=900))
    flat_text = format_context(ctx)
    # 「只要 6 条」与「要覆盖面」是两种需求，成本必须分开测 ——
    # 拿 k=6 的扁平去和默认给全貌的树比，是在比较两个不同的东西。
    wide_ms, wide_ctx = _timed(
        lambda: build_context(palace, query="仓位怎么分配", budget=4000, limit=30)
    )
    wide_text = format_context(wide_ctx)
    tree_ms, tree = _timed(lambda: (
        lambda t: render_tree(t, expand=2, budget=900, leaf_limit=4)
    )(build_tree(palace, scores={r["name"]: r["score"] for r in palace.score_breakdown("仓位怎么分配")})))

    cycle_ms, _ = _timed(lambda: palace.run_cycle())
    load_ms, loaded = _timed(lambda: MemoryPalace(path=store, clock=MutableClock()))
    reload_query_ms, _ = _timed(lambda: loaded.recall(query="仓位怎么分配"))

    # 单次调用延迟才是 agent 真正感知的东西：每一步都会 remember 一笔。
    # 取中位数而不是均值：一次 GC 或一次 fsync 抖动就能把均值拉歪。
    per_write = []
    autosave = MemoryPalace(path=os.path.join(tmp, "lat.sqlite"), clock=MutableClock(), autosave=True)
    for i in range(25):
        ms, _ = _timed(lambda i=i: autosave.remember(f"lat{i}", f"延迟样本 {i} 仓位与复盘", tags=("压测",)))
        per_write.append(ms)
    per_write.sort()
    write_p50 = per_write[len(per_write) // 2]
    write_p95 = per_write[int(len(per_write) * 0.95) - 1]
    # 改一条：增量写应当与库的大小基本无关
    update_ms, _ = _timed(lambda: autosave.remember("lat3", "延迟样本 3 仓位与复盘（改一条）", tags=("压测",)))

    flat_chars = len(flat_text)
    tree_chars = len(tree["text"])
    return {
        "nodes": len(palace.graph),
        "write_ms": round(write_ms, 1),
        "write_per_node_ms": round(write_ms / max(1, n), 3),
        "save_ms": round(save_ms, 1),
        "load_ms": round(load_ms, 1),
        "query_cold_ms": round(cold_ms, 1),
        "query_warm_ms": round(warm_ms, 1),
        "query_after_load_ms": round(reload_query_ms, 1),
        "write_p50_ms": round(write_p50, 2),
        "write_p95_ms": round(write_p95, 2),
        "update_one_ms": round(update_ms, 2),
        "context_ms": round(ctx_ms, 1),
        "context_tree_ms": round(tree_ms, 1),
        "cycle_ms": round(cycle_ms, 1),
        "flat_chars": flat_chars,
        "flat_items": len(ctx["items"]),
        "wide_flat_chars": len(wide_text),
        "wide_flat_items": len(wide_ctx["items"]),
        "wide_flat_ms": round(wide_ms, 1),
        "tree_chars": tree_chars,
        "tree_named": tree["folded_nodes"] + tree["expanded"] * 4 + tree["roots"],
        "saving_vs_wide": round(1 - tree_chars / max(1, len(wide_text)), 3),
        "tree_shape": tree_stats(build_tree(palace)),
        "store_kb": round(os.path.getsize(store) / 1024, 1),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="memory-palace-bench", description="记忆系统基准")
    parser.add_argument("sizes", nargs="*", type=int, default=[200, 1000])
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    sizes = args.sizes or [200, 1000]

    rows = []
    for n in sizes:
        row = run_one(n)
        rows.append(row)
        if not args.json:
            print(
                f"[{n:>5} 条] 写入 {row['write_ms']:>7.1f}ms（{row['write_per_node_ms']}ms/条）"
                f"｜存 {row['save_ms']:>6.1f}ms｜读 {row['load_ms']:>6.1f}ms"
            )
            print(
                f"        查询 冷 {row['query_cold_ms']:>6.1f}ms / 热 {row['query_warm_ms']:>6.1f}ms"
                f"｜context 平铺 {row['context_ms']:>6.1f}ms / 树 {row['context_tree_ms']:>6.1f}ms"
                f"｜cycle {row['cycle_ms']:>6.1f}ms"
            )
            print(
                f"        单次写入 p50 {row['write_p50_ms']:>6.2f}ms / p95 {row['write_p95_ms']:>6.2f}ms"
                f"｜改一条 {row['update_one_ms']:>5.2f}ms"
            )
            print(
                f"        注入 扁平(6条) {row['flat_chars']:>4} 字 / 扁平(30条) {row['wide_flat_chars']:>5} 字"
                f" / 树 {row['tree_chars']:>4} 字（比 30 条省 {row['saving_vs_wide']:.0%}）"
                f"｜枝 {row['tree_shape']['branches']}｜库 {row['store_kb']}KB"
            )
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
