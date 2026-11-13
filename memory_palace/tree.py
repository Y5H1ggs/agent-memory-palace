"""tree.py - 树状记忆：根常驻、枝按需展开、其余折叠。

扁平注入的问题是**每条记忆都要付全额 token**。12 条时还能忍，120 条时有两条路：
要么截断（丢掉刚需），要么全塞（爆预算）。树状结构给第三条路：**分层付费**。

    ├─ 硬规则（2，钉死）         ← 根：永远在，两条一句话
    ├─ 仓位（3）                 ← 命中的枝：展开到叶子
    │  · 半凯利：单笔 ≤ 1/3
    │  · 资金管理：分散不押单只
    └─ 复盘（4）· 缩量反弹不追高…（另有 3 条）   ← 没命中的枝：只留摘要

三件事必须说清楚，否则「树」只是换个排版：

1. **树是派生的，不是存的。** 不新增节点类型、不改 state 版本 ——
   枝 = 沉淀父节点或首个标签，叶 = 记忆本身。改动图结构时树自动跟着变。
2. **折叠是有损的，必须标出来。** `（另有 3 条）` 不是装饰：它明确告诉模型
   「这枝还有东西，需要时再展开」，而不是假装这就是全部。
3. **付费顺序按「先根、再相关的枝、最后背景」。** 预算紧时先砍背景枝，
   永远不砍根 —— 硬规则的价在于「不相关时也得遵守」。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from .core import MemoryNode

TIER_LABEL = {
    "core": "核心",
    "fact": "事实",
    "event": "经历",
    "ephemeral": "临时",
    "trace": "碎片",
}


@dataclass
class Branch:
    key: str
    label: str
    nodes: list[MemoryNode] = field(default_factory=list)
    mass: float = 0.0
    relevance: float = 0.0
    consolidated: bool = False

    @property
    def size(self) -> int:
        return len(self.nodes)

    @property
    def best(self) -> MemoryNode | None:
        return self.nodes[0] if self.nodes else None


@dataclass
class MemoryTree:
    rules: list[MemoryNode]
    branches: list[Branch]
    now: float
    total: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "roots": len(self.rules),
            "branches": [
                {
                    "key": b.key,
                    "label": b.label,
                    "size": b.size,
                    "mass": round(b.mass, 4),
                    "relevance": round(b.relevance, 4),
                    "consolidated": b.consolidated,
                }
                for b in self.branches
            ],
        }


def _is_root(node: MemoryNode) -> bool:
    """根 = 钉死 + 身份类。它们是判据，不是被检索的内容。"""
    return node.pinned or node.kind == "identity" or node.tier == "core" and not node.consolidated_into


def _branch_key(node: MemoryNode, graph) -> tuple[str, str, bool]:
    """一条记忆挂在哪个枝上：优先跟随沉淀父节点，其次首个标签，最后按层级兜底。"""
    if node.consolidated_into:
        parent = graph.get(node.consolidated_into)
        if parent is not None:
            label = parent.tags[0] if parent.tags else "沉淀"
            return f"c:{parent.name}", label, True
    if node.tags:
        return f"t:{node.tags[0]}", node.tags[0], False
    return f"x:{node.tier}", TIER_LABEL.get(node.tier, node.tier), False


def build_tree(palace, now: float | None = None, scores: dict[str, float] | None = None) -> MemoryTree:
    """从图派生一棵两层树。`scores` 是查询相关度（可缺省，缺省时只按重要性）。"""
    moment = palace.now() if now is None else now
    assessment = palace.assessment(moment)
    scores = scores or {}
    roots: list[MemoryNode] = []
    buckets: dict[str, Branch] = {}

    for node in palace.graph.all_nodes():
        if _is_root(node):
            roots.append(node)
            continue
        key, label, consolidated = _branch_key(node, palace.graph)
        branch = buckets.get(key)
        if branch is None:
            branch = Branch(key=key, label=label, consolidated=consolidated)
            buckets[key] = branch
        branch.nodes.append(node)
        branch.mass += assessment.get(node.name, {}).get("total", 0.0)
        branch.relevance = max(branch.relevance, scores.get(node.name, 0.0))

    roots.sort(key=lambda n: (not n.pinned, -assessment.get(n.name, {}).get("total", 0.0), n.name))
    branches = list(buckets.values())
    for branch in branches:
        branch.nodes.sort(
            key=lambda n: (-assessment.get(n.name, {}).get("total", 0.0), n.name)
        )
        # 一个枝的相关度取「最强那条」而不是平均：命中一条就该展开这一枝
        if branch.relevance == 0.0 and scores:
            branch.relevance = max(
                (scores.get(n.name, 0.0) for n in branch.nodes), default=0.0
            )
    branches.sort(key=lambda b: (-b.relevance, -b.mass, b.label))
    return MemoryTree(rules=roots, branches=branches, now=moment, total=len(palace.graph))


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


def _rule_line(node: MemoryNode, limit: int) -> str:
    marks = "钉死" if node.pinned else "根"
    return f"├─ [{marks}] {_clip(node.content, limit)}"


def render_tree(
    tree: MemoryTree,
    expand: int = 1,
    budget: int = 700,
    rule_chars: int = 46,
    leaf_chars: int = 34,
    leaf_limit: int = 3,
    max_branches: int = 5,
    gist_chars: int = 22,
) -> dict[str, Any]:
    """渲染成注入块。expand = 展开几个枝，其余折叠；超过 max_branches 的主题合并成一行。

    两个参数都是被基准逼出来的：
      · 枝多时每枝留一句摘要，加起来比扁平还长 —— 所以主题数必须封顶，
        超出的合并为「其余 N 个主题（M 条）」，而不是逐枝列摘要。
      · gist 只留 22 字：折叠的枝只需要「提醒这里有什么」，不需要复述内容。
    """
    lines: list[str] = []
    used = 0
    spent_roots = 0

    for node in tree.rules:
        line = _rule_line(node, rule_chars)
        if used + len(line) > budget and spent_roots:
            break
        lines.append(line)
        used += len(line)
        spent_roots += 1

    folded_nodes: list[str] = []
    opened = 0
    closed = 0
    visible = 0
    overflow: list[Branch] = []

    for branch in tree.branches:
        if visible >= max_branches:
            overflow.append(branch)
            folded_nodes.extend(n.name for n in branch.nodes)
            continue
        visible += 1
        is_open = opened < expand and branch.relevance > 0
        if is_open:
            head = f"├─ {branch.label}（{branch.size}）"
            lines.append(head)
            used += len(head)
            opened += 1
            shown = 0
            for node in branch.nodes[:leaf_limit]:
                line = f"│  · {_clip(node.content, leaf_chars)}"
                if used + len(line) > budget:
                    break
                lines.append(line)
                used += len(line)
                shown += 1
            rest = branch.size - shown
            if rest > 0:
                tail = f"│  … 另有 {rest} 条（recall 取）"
                lines.append(tail)
                used += len(tail)
                folded_nodes.extend(n.name for n in branch.nodes[shown:])
        else:
            gist = _clip(branch.best.content, gist_chars) if branch.best else ""
            tail = f"（另有 {branch.size - 1} 条）" if branch.size > 1 else ""
            line = f"├─ {branch.label}（{branch.size}）· {gist} {tail}".rstrip()
            if used + len(line) > budget and (spent_roots or opened):
                visible -= 1
                overflow.append(branch)
                folded_nodes.extend(n.name for n in branch.nodes)
                continue
            lines.append(line)
            used += len(line)
            closed += 1
            folded_nodes.extend(n.name for n in branch.nodes[1:])

    if overflow:
        total_folded = sum(b.size for b in overflow)
        names = "、".join(b.label for b in overflow[:4])
        more = "…" if len(overflow) > 4 else ""
        line = f"└─ 其余 {len(overflow)} 个主题（{total_folded} 条）：{names}{more}"
        lines.append(line)
        used += len(line)
        closed += len(overflow)

    body = "\n".join(lines) if lines else "（空）"
    header = (
        f"# 记忆树（{tree.total} 条 → 根 {len(tree.rules)} / 枝 {len(tree.branches)}，"
        f"展开 {min(opened, len(tree.branches))} 枝，{used}/{budget} 字）"
    )
    return {
        "text": header + "\n" + body,
        "used": used,
        "budget": budget,
        "roots": len(tree.rules),
        "branches": len(tree.branches),
        "expanded": opened,
        "folded": closed,
        "folded_nodes": len(folded_nodes),
        "folded_names": folded_nodes,
    }


def tree_stats(tree: MemoryTree) -> dict[str, Any]:
    """给 size / bench 用的形状读数。"""
    sizes = [b.size for b in tree.branches]
    return {
        "roots": len(tree.rules),
        "branches": len(tree.branches),
        "largest_branch": max(sizes, default=0),
        "leaf_total": sum(sizes),
        "shape": "single-trunk" if len(sizes) <= 1 else ("flat" if max(sizes, default=0) <= 2 else "branched"),
    }


def iter_leaves(tree: MemoryTree) -> Iterable[MemoryNode]:
    for branch in tree.branches:
        yield from branch.nodes
