"""value.py - 选择：记住什么、遗忘什么，以及「选择」本身的形状。

上一版的遗忘是一条物理定律：权重低于阈值就消失。可用户说的是「我选择」——
选择不是余量归零，是有价值判断的取舍，且必须可解释、可审计、有预算。

四件事：
  · importance：一条记忆值不值得留，由 强度 / 情绪 余温 / 逻辑凭据 / 叙事地位 合成
  · entropy：整体的「重要性分布」有多平。全平 = 什么都一样重要 = 什么都不重要
  · structure：分布之外还要看**图的形状** —— 全是孤岛、还是一个中心吸走所有边
  · grasp：每轮只能抓固定预算，抓谁按重要性排 —— 存量膨胀时，预算被摊薄
"""
from __future__ import annotations

import math
from typing import Any, Iterable

from .core import MemoryGraph, MemoryNode


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


class ValueModel:
    """价值判断层：把「值不值得留」变成一个可拆解、可反驳的算式。"""

    def __init__(self, config: Any) -> None:
        self.config = config

    # ---------- 重要性 ----------

    def importance(
        self, node: MemoryNode, now: float, support: float = 0.0
    ) -> dict[str, float]:
        cfg = self.config
        weight = _clamp(node.effective_weight(now, cfg.affect_stability_gain))
        affect = _clamp(node.affect.residue(now))
        support_n = _clamp(support)
        # 叙事地位读的是「退潮后的地位」，不是累计次数：讲过一次不会终身重要。
        central = node.narrative_status(
            now, cfg.narrative_centrality_cap, cfg.narrative_heat_half_life_days
        )["central"]
        # 线性加权、不做归一化：这样「纯物理强度」的节点 importance ≈ weight，
        # 与老的 forget_w 口径可比；情绪 / 凭据 / 叙事是**加成**，而不是互相摊薄。
        total = (
            cfg.importance_w_weight * weight
            + cfg.importance_w_affect * affect
            + cfg.importance_w_support * support_n
            + cfg.importance_w_central * central
        )
        return {
            "weight": round(weight, 6),
            "affect": round(affect, 6),
            "support": round(support_n, 6),
            "central": round(central, 6),
            "total": round(total, 6),
        }

    def assess(
        self, graph: MemoryGraph, now: float, support_map: dict[str, float] | None = None
    ) -> dict[str, dict[str, float]]:
        support_map = support_map or {}
        return {
            node.name: self.importance(node, now, support_map.get(node.name, 0.0))
            for node in graph.all_nodes()
        }

    # ---------- 遗忘：一次可审计的取舍 ----------

    def gc_decisions(
        self, graph: MemoryGraph, now: float, support_map: dict[str, float] | None = None
    ) -> list[dict[str, Any]]:
        """每条记忆都拿到一个判决 + 理由 + 四个分量的原始值。

        「被忘记」从此不是「掉到零了」，而是「四个分量加起来不足以说明它该留下」。

        两类记忆适用两套遗忘律，这是刻意的：
          · 结论（core / fact / event）按**价值**取舍 —— 记得住但没价值，也可以主动放手；
          · 碎片（已沉淀的 trace）按**物理强度**退场 —— 它已经把价值交出去了，
            剩下的只是一个溯源窗口。若也拿价值门槛去卡它，结晶当轮就会把自己的
            出处全部抹掉，那不是有原则的遗忘，是失忆。
        """
        cfg = self.config
        support_map = support_map or {}
        decisions: list[dict[str, Any]] = []
        for node in graph.all_nodes():
            parts = self.importance(node, now, support_map.get(node.name, 0.0))
            delegated = node.tier == "trace" or bool(node.consolidated_into)
            if node.pinned:
                verdict, reason = "kept", "pinned_by_user"
            elif parts["weight"] <= cfg.forget_w:
                verdict, reason = "forgotten", "weight_floor"
            elif delegated:
                verdict, reason = "kept", "delegated_to_core"
            elif parts["total"] < cfg.importance_forget:
                verdict, reason = "forgotten", "importance_below_threshold"
            else:
                verdict, reason = "kept", "valued"
            decisions.append(
                {
                    "name": node.name,
                    "verdict": verdict,
                    "reason": reason,
                    "delegated": delegated,
                    **parts,
                }
            )
        return decisions

    # ---------- 熵：对抗熵增，不等于把一切冻住 ----------

    def distribution(self, assessment: dict[str, dict[str, float]]) -> dict[str, float]:
        """重要性分布的三个读数。

        Shannon 熵留作诊断，但**不用它做判据**：小样本下它几乎没有分辨力
        （10 个节点、3 倍差距，H 只从 1.0 掉到 0.98）。判断「有没有梯度」得看份额。

        top_share 太小 → 什么都一样重要 → 没有「我」；
        top_share 太大 → 一切都押在一件事上 → 也没有分辨力。
        """
        totals = sorted((v["total"] for v in assessment.values() if v["total"] > 0), reverse=True)
        if not totals:
            return {"entropy": 0.0, "top_share": 0.0, "top3_share": 0.0, "effective_n": 0.0}
        mass = sum(totals)
        probs = [t / mass for t in totals]
        h = -sum(p * math.log(p) for p in probs if p > 0)
        return {
            "entropy": round(h / math.log(len(totals)), 6) if len(totals) > 1 else 0.0,
            "top_share": round(totals[0] / mass, 6),
            "top3_share": round(sum(totals[:3]) / mass, 6),
            "effective_n": round(math.exp(h), 4),
        }

    def entropy(self, assessment: dict[str, dict[str, float]]) -> float:
        return self.distribution(assessment)["entropy"]

    def health(self, dist: dict[str, float]) -> str:
        """健康不是一个点，是一段区间：太平与太陡，都是自我的消失。

        只接受分布本身，不接受「只给一个熵」的调用 —— 那会因为缺少份额而判错。
        """
        cfg = self.config
        if dist["top3_share"] < cfg.entropy_flat_below:
            return "flat"
        if dist["top_share"] > cfg.entropy_narrow_above:
            return "narrow"
        return "healthy"

    # ---------- 结构：分布之外，图有没有形状 ----------

    def structural(self, graph: MemoryGraph, now: float = 0.0) -> dict[str, float]:
        """只看重要性会把「十座孤岛」判成健康：价值有梯度，但彼此毫无关联。

        三个读数各管一件事，不合成一个数：
          · degree_entropy  边的分布有多平（都挂在一个中心上 → 很低）
          · isolated_share  有多少条目一根边都没有（记下了，但从未与任何东西相连）
          · fragmentation   连通分量占节点数的比例（全是孤岛 → 1.0）
        """
        nodes = graph.all_nodes()
        n = len(nodes)
        if n == 0:
            return {
                "degree_entropy": 0.0,
                "isolated_share": 0.0,
                "components": 0.0,
                "fragmentation": 0.0,
                "giant_share": 0.0,
            }
        degree = {node.name: 0.0 for node in nodes}
        adjacency: dict[str, set[str]] = {node.name: set() for node in nodes}
        for a, b, edge in graph.edges():
            if a in degree and b in degree:
                degree[a] += edge.weight
                degree[b] += edge.weight
                adjacency[a].add(b)
                adjacency[b].add(a)

        isolated = sum(1 for value in degree.values() if value <= 0.0)
        total = sum(degree.values())
        if total > 0 and n > 1:
            probs = [value / total for value in degree.values() if value > 0]
            h = -sum(p * math.log(p) for p in probs if p > 0)
            degree_entropy = h / math.log(n)
        else:
            degree_entropy = 0.0

        seen: set[str] = set()
        sizes: list[int] = []
        for name in adjacency:
            if name in seen:
                continue
            stack = [name]
            seen.add(name)
            size = 0
            while stack:
                cur = stack.pop()
                size += 1
                for nb in adjacency[cur]:
                    if nb not in seen:
                        seen.add(nb)
                        stack.append(nb)
            sizes.append(size)

        return {
            "degree_entropy": round(degree_entropy, 6),
            "isolated_share": round(isolated / n, 6),
            "components": float(len(sizes)),
            "fragmentation": round(len(sizes) / n, 6),
            "giant_share": round(max(sizes) / n, 6) if sizes else 0.0,
        }

    def structure_health(self, struct: dict[str, float]) -> str:
        """结构健康与价值健康分开判：一个系统可以「价值有梯度」但「结构是散的」。"""
        cfg = self.config
        if struct.get("components", 0.0) <= 0.0:
            return "empty"  # 没有节点时不能说它「单枢纽」，那是把空库判成病
        if struct.get("components", 0.0) <= 1.0:
            return "connected" if struct.get("degree_entropy", 0.0) >= cfg.structure_hub_below else "hub"
        if struct.get("isolated_share", 0.0) >= cfg.structure_isolated_warn:
            return "isolated"
        if struct.get("fragmentation", 0.0) >= cfg.structure_fragment_warn:
            return "fragmented"
        if struct.get("degree_entropy", 0.0) < cfg.structure_hub_below:
            return "hub"
        return "connected"

    # ---------- 代谢预算：拼命抓住，但只有两只手 ----------

    def grasp(
        self,
        graph: MemoryGraph,
        now: float,
        support_map: dict[str, float] | None = None,
        budget: float | None = None,
    ) -> list[dict[str, Any]]:
        """按重要性排序，用有限预算逐条「抓住」。

        抓的是结论，不是原始碎片：已沉淀（trace / 已归属某条结晶）的不再单独立、
        也不额外加固 —— 否则单门沉淀会被预算偷偷破掉。
        """
        cfg = self.config
        support_map = support_map or {}
        remaining = cfg.grasp_budget if budget is None else budget
        candidates: list[tuple[float, str]] = []
        for node in graph.all_nodes():
            if node.pinned or node.tier == "trace" or node.consolidated_into:
                continue
            parts = self.importance(node, now, support_map.get(node.name, 0.0))
            candidates.append((parts["total"], node.name))
        candidates.sort(key=lambda pair: (-pair[0], pair[1]))

        spent_log: list[dict[str, Any]] = []
        for total, name in candidates:
            if remaining <= 1e-9:
                break
            node = graph.get(name)
            if node is None:
                continue
            units = min(1.0, remaining)
            node.access(now, cfg.grasp_gain * units)
            remaining -= units
            spent_log.append(
                {"name": name, "spent": round(units, 4), "importance": round(total, 6)}
            )
        # 余量不浪费：候选不够时，把剩下的力气全使在最要紧的那条上。
        if remaining > 1e-9 and spent_log:
            top = graph.get(spent_log[0]["name"])
            if top is not None:
                top.access(now, cfg.grasp_gain * remaining)
                spent_log[0]["spent"] = round(spent_log[0]["spent"] + remaining, 4)
        return spent_log

    def ungrabbed(self, graph: MemoryGraph, grasp_log: Iterable[dict[str, Any]]) -> list[str]:
        """本轮没被抓住的记忆 —— 它们只能靠自己衰减后的强度撑着。"""
        grabbed = {row["name"] for row in grasp_log}
        return sorted(n.name for n in graph.all_nodes() if n.name not in grabbed)
