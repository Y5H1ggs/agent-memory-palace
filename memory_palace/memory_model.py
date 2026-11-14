"""memory_model.py - 一套记忆模型：不是一种衰减律，而是五种寿命。

之前只有 tier（trace/event/fact/core），那是**重要性分级**，回答「这条重不重要」。
但「重不重要」和「该记多久、该怎么被想起来」是两回事：

  工作记忆  此刻手上拿着的。容量极小，不遗忘而是被挤出去。
  情景记忆  某一刻发生过什么。带时间与情绪，中等寿命。
  语义记忆  去时间化的知识。慢，但有凭据就极稳。
  程序记忆  怎么做事。几乎不衰减，因为你每天都在用它。
  身份记忆  我是谁。不参与遗忘，只能修宪。

同一件事在这五层里寿命可以差三个数量级 —— 这才是「一套模型」而不是「一个公式」。

两条不变量：
  1. 工作记忆超容量时**降级**为情景记忆，而不是删除。挤出去的东西还在，只是不在手边了。
  2. 身份记忆不参与 GC。它是判据，不是被判断的对象。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .core import MemoryGraph, MemoryNode, PalaceConfig


@dataclass(frozen=True)
class MemoryKind:
    key: str
    label: str
    stability_mult: float  # 乘在衰减稳定性上：越大越难忘
    capacity: int | None  # 容量上限；None = 不限
    retrieval_bias: float  # 检索时的影响力权重
    volatile: bool  # 是否每轮被容量挤出
    forgettable: bool  # 是否参与 GC


KINDS: dict[str, MemoryKind] = {
    "working": MemoryKind("working", "工作记忆", 0.05, 7, 1.30, True, False),
    "episodic": MemoryKind("episodic", "情景记忆", 1.00, None, 1.00, False, True),
    "semantic": MemoryKind("semantic", "语义记忆", 2.60, None, 0.95, False, True),
    "procedural": MemoryKind("procedural", "程序记忆", 6.00, None, 1.05, False, True),
    "identity": MemoryKind("identity", "身份记忆", 1e6, None, 1.20, False, False),
}

KIND_ORDER: tuple[str, ...] = (
    "working",
    "episodic",
    "semantic",
    "procedural",
    "identity",
)

# core.MemoryNode.stability() 通过这个表取倍率，避免两处各写一份真理
KIND_STABILITY: dict[str, float] = {k: v.stability_mult for k, v in KINDS.items()}

_PROCEDURAL_TAGS = {"习惯", "流程", "程序", "惯例", "方法", "sop"}
_IDENTITY_TAGS = {"身份", "基座", "自我", "人设", "硬规则"}


def kind_spec(key: str) -> MemoryKind:
    if key not in KINDS:
        raise KeyError(f"未知记忆类型: {key!r}（可选 {sorted(KINDS)}）")
    return KINDS[key]


def classify(node: MemoryNode) -> str:
    """显式标注优先；没标注的按属性推断 —— 但推断结果从不覆盖已有标注。"""
    if node.kind in KINDS:
        return node.kind
    tags = set(node.tags)
    if node.pinned or node.tier == "core" or tags & _IDENTITY_TAGS:
        return "identity"
    if tags & _PROCEDURAL_TAGS:
        return "procedural"
    if node.origin == "consolidated" or node.tier == "fact" or node.consolidated_into:
        return "semantic"
    if node.tier == "ephemeral":
        return "working"
    return "episodic"


class MemoryModel:
    """把节点分配到记忆类型，并执行各类型的资源规则。"""

    def __init__(self, config: PalaceConfig) -> None:
        self.config = config
        self.last_evictions: list[dict[str, Any]] = []

    # ---- 分配 ----

    def enroll(self, node: MemoryNode) -> str:
        """写入时定类型。没指定就按属性推断，写回节点，避免每次都重算。"""
        node.kind = classify(node)
        return node.kind

    # ---- 工作记忆容量 ----

    def evict_working(
        self, graph: MemoryGraph, now: float, decay_floor: float = 0.0
    ) -> list[dict[str, Any]]:
        """工作记忆退场有两条路，都不是删除：

        1. 拿不住了 —— 强度衰到 floor 以下，自动沉为情景记忆（还在，只是不在手边）；
        2. 挤出去了 —— 超过容量 7，最弱的沉为情景记忆。
        「挤出」和「遗忘」是两件事：前者是注意力有限，后者是价值不足。
        混成一件，就会把「刚才还在想的事」当成「不值得记的事」清掉。
        """
        log: list[dict[str, Any]] = []
        working = [n for n in graph.all_nodes() if n.kind == "working"]
        if decay_floor > 0:
            for node in working:
                if node.effective_weight(now) <= decay_floor:
                    node.kind = "episodic"
                    node.tier = "event" if node.tier == "ephemeral" else node.tier
                    log.append(
                        {
                            "name": node.name,
                            "action": "demote",
                            "reason": "decayed",
                            "from": "working",
                            "to": "episodic",
                        }
                    )
            working = [n for n in graph.all_nodes() if n.kind == "working"]

        cap = KINDS["working"].capacity or 0
        if len(working) > cap:
            working.sort(key=lambda n: (-n.effective_weight(now), n.name))
            for node in working[cap:]:
                node.kind = "episodic"
                node.tier = "event" if node.tier == "ephemeral" else node.tier
                log.append(
                    {
                        "name": node.name,
                        "action": "demote",
                        "reason": "capacity",
                        "from": "working",
                        "to": "episodic",
                    }
                )
        self.last_evictions = log
        return log

    # ---- 检索偏向 ----

    def bias(self, node: MemoryNode) -> float:
        """不同记忆类型被想起的难易不同：手上 > 身份 > 程序 > 情景 > 语义。"""
        return kind_spec(node.kind).retrieval_bias

    # ---- 健康度 ----

    def composition(self, graph: MemoryGraph) -> dict[str, Any]:
        nodes = graph.all_nodes()
        total = len(nodes) or 1
        counts = {k: 0 for k in KIND_ORDER}
        for n in nodes:
            counts[classify(n)] = counts.get(classify(n), 0) + 1
        shares = {k: round(v / total, 4) for k, v in counts.items()}
        return {"counts": counts, "shares": shares, "total": len(nodes)}

    def health(self, comp: dict[str, Any]) -> str:
        """只有情景 = 记得所有事却没学到任何东西；只有语义 = 全是道理却没有经历。"""
        shares = comp["shares"]
        if comp["total"] == 0:
            return "empty"
        if shares.get("semantic", 0.0) + shares.get("procedural", 0.0) < 0.02:
            return "no_abstraction"
        if shares.get("episodic", 0.0) < 0.02:
            return "no_experience"
        return "balanced"

    def describe(self, graph: MemoryGraph) -> dict[str, Any]:
        comp = self.composition(graph)
        return {**comp, "health": self.health(comp), "kinds": list(KIND_ORDER)}
