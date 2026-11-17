"""test_layers.py - 两级注入的契约：该省的地方省，该全的地方全。

「省 token」有两种做法，差别很大：

  · 少给几条 —— 覆盖率下降，模型开始猜；
  · 给全，但大部分只给**索引** —— 覆盖率不变，字数降一个量级。

这个文件钉的是第二种。它最容易坏在两处：
  1. 条目少的时候也硬加索引，成本比收益高（索引本身也是字）；
  2. 索引装不下时**静默少报**，让模型以为库就这么大 —— 这比不给更危险。
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from memory_palace import MemoryPalace, MutableClock  # noqa: E402
from memory_palace.cli import _clip_index, build_context, format_context  # noqa: E402

TOPICS = [
    ("工程", "存储"), ("工程", "检索"), ("工程", "并发"),
    ("复盘", "教训"), ("偏好", "输出"), ("交易", "仓位"),
    ("交易", "板块"), ("生活", "咖啡"), ("生活", "养猫"), ("学习", "方法"),
]


def _palace(n: int) -> MemoryPalace:
    palace = MemoryPalace(clock=MutableClock())
    for i in range(n):
        tags = TOPICS[i % len(TOPICS)]
        palace.remember(
            f"n{i}",
            f"第 {i} 条：{tags[0]}方面关于{tags[1]}的结论，带具体依据",
            tags=tags,
            tier="fact" if i % 3 else "event",
        )
    palace.semantics.sync(palace.graph)
    return palace


def test_small_palace_gets_no_index():
    """8 条以内不加索引：索引本身也要字，小库直接给更清楚。"""
    ctx = build_context(_palace(6), query="存储", budget=600)
    assert ctx["items"], "小库应当直接给正文"
    assert ctx["index"] == [], "小库不该有索引尾巴"


def test_medium_palace_lists_items_one_by_one():
    palace = _palace(22)
    ctx = build_context(palace, query="存储", budget=600)
    assert 0 < len(ctx["index"]) <= 20, "中等规模逐条列，不聚合"
    assert all(not i["name"].startswith("tag:") for i in ctx["index"])


def test_large_palace_uses_category_index_and_covers_all():
    """大库按标签聚合，且覆盖数必须等于「未展开」总数 —— 不许少报。"""
    palace = _palace(60)
    ctx = build_context(palace, query="存储", budget=400)
    grouped = [i for i in ctx["index"] if i["name"].startswith("tag:")]
    assert grouped, "60 条应当走分类索引"
    covered = sum(int(i.get("count") or 1) for i in ctx["index"])
    dropped = ctx["total_candidates"] - len(ctx["items"])
    assert covered == dropped, f"覆盖 {covered} 条，但未展开 {dropped} 条 —— 少报了"


def test_index_mode_covers_more_than_full_mode():
    """index 层的意义：同样预算下覆盖更多条。"""
    palace = _palace(40)
    full = build_context(palace, query="工程", budget=500)
    brief = build_context(palace, query="工程", budget=500, level="index")
    assert len(brief["items"]) > len(full["items"])
    assert brief["used"] <= 500


def test_budget_visibility_warns_near_limit():
    """快撞墙时要说出来，而不是让调用方下次自己撞。"""
    palace = _palace(20)
    ctx = build_context(palace, query="存储", budget=260)
    joined = " ".join(ctx["warnings"])
    if ctx["used"] >= 260 * 0.9:
        assert "预算已用" in joined


def test_index_lines_carry_ellipsis():
    """截断必须带省略号：没有省略号的截断比不截断更误导。"""
    long_text = "这是一条很长的记忆内容，用来测试索引行在超出长度时会不会带上省略号"
    clipped = _clip_index(long_text, 20)
    assert clipped.endswith("…")
    assert len(clipped) <= 21


def test_index_block_is_rendered_before_warnings():
    palace = _palace(60)
    text = format_context(build_context(palace, query="存储", budget=400))
    assert "## 索引" in text
    assert text.index("## 索引") < text.index("## 提示")
