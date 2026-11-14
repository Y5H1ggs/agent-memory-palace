"""consolidation.py - 记忆沉淀：把反复共现的碎片压成稳定的语义节点。

判据不是「访问次数多」，而是「结构上一伙 + 语义上同源 + 还活着」。
沉淀是单向门：碎片降级为 trace 并指向沉淀节点，不会被反复重复压榨。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Callable

from .core import Event, MemoryGraph, MemoryNode, PalaceConfig
from .semantics import cosine

Summarizer = Callable[[list[MemoryNode]], str]
VectorLookup = Callable[[MemoryNode], list[float] | None]


def default_summary(members: list[MemoryNode]) -> str:
    shared = sorted(set.intersection(*[set(m.tags) for m in members])) if members else []
    label = "交集" if shared else "并集"
    if not shared and members:
        shared = sorted(set().union(*[set(m.tags) for m in members]))[:3]
    points: list[str] = []
    for m in members:
        text = m.content.strip()
        if text and text not in points:
            points.append(text)
    topic = "、".join(shared) if shared else "未命名主题"
    return (
        f"[沉淀] 主题={topic}（{label}）| 来源={len(members)}条 | 要点："
        + "；".join(points[:4])
    )


@dataclass
class ConsolidationResult:
    created: list[str] = field(default_factory=list)
    reused: list[str] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    clusters_seen: int = 0
    clusters_skipped: int = 0
    by_tags: int = 0
    by_semantics: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "created": self.created,
            "reused": self.reused,
            "clusters_seen": self.clusters_seen,
            "clusters_skipped": self.clusters_skipped,
            "by_tags": self.by_tags,
            "by_semantics": self.by_semantics,
        }


class ConsolidationEngine:
    def __init__(
        self,
        config: PalaceConfig,
        summarizer: Summarizer | None = None,
        vector_lookup: VectorLookup | None = None,
    ) -> None:
        self.config = config
        self.summarizer = summarizer or default_summary
        self.vector_lookup = vector_lookup
        # 一轮聚类里每个节点只取一次向量。否则 N² 对候选会触发 2N² 次 lookup，
        # 每次都过一个 sync 检查 —— 200 条就是 4 万次，直接吃掉 200ms。
        self._vec_cache: dict[str, list[float] | None] = {}

    @staticmethod
    def _fingerprint(names: list[str]) -> str:
        digest = hashlib.sha1(",".join(sorted(names)).encode("utf-8")).hexdigest()
        return "c:" + digest[:12]

    def _vector(self, node: MemoryNode) -> list[float] | None:
        if self.vector_lookup is None:
            return None
        if node.name not in self._vec_cache:
            self._vec_cache[node.name] = self.vector_lookup(node) or None
        return self._vec_cache[node.name]

    def _similarity(self, a: MemoryNode, b: MemoryNode, graph: MemoryGraph) -> float:
        """标签同源 或 内容同义，任一成立即视为可聚类。

        只靠标签会把「同一件事换了说法」拆成两堆；只靠向量又会把模板化的
        空话连成一团。两条通道取较大者，任一路径够近就认。
        """
        ta, tb = set(a.tags), set(b.tags)
        jaccard = len(ta & tb) / max(1, len(ta | tb))
        edge = graph.edge(a.name, b.name)
        co = min(5, edge.coactivations if edge else 0) / 5.0
        structural = 0.75 * jaccard + 0.25 * co
        if self.vector_lookup is None:
            return structural
        va, vb = self._vector(a), self._vector(b)
        if not va or not vb:
            return structural
        return max(structural, 0.85 * cosine(va, vb))

    def _pairs(self, candidates: list[MemoryNode]) -> list[tuple[MemoryNode, MemoryNode]]:
        """要比较的候选对。

        小规模（≤ block_threshold）全对比较 —— 语义聚类必须能跨标签发现同义碎片，
        分桶会把「换了说法」的碎片拆开（`第 2.5 幕` 就是要防这个）。
        大规模才分桶：按标签分，无标签按层级分。这是**有损**的近似 ——
        代价是「完全不同的标签、但内容同义」的碎片在超大库里可能沉淀不到一起。
        我们选择显式承认这个代价：一次 run_cycle 从 1.6 秒降到几十毫秒，
        比偶尔漏掉一对同义碎片更值。
        """
        cfg = self.config
        n = len(candidates)
        if n <= cfg.consolidate_block_threshold:
            return [
                (a, b)
                for i, a in enumerate(candidates)
                for b in candidates[i + 1 :]
            ]

        buckets: dict[str, list[MemoryNode]] = {}
        for node in candidates:
            keys = set(node.tags) or {f"tier:{node.tier}"}
            for key in keys:
                buckets.setdefault(key, []).append(node)
        pairs: list[tuple[MemoryNode, MemoryNode]] = []
        seen: set[tuple[str, str]] = set()
        budget = cfg.consolidate_max_pairs
        for members in buckets.values():
            for i, a in enumerate(members):
                for b in members[i + 1 :]:
                    key = (a.name, b.name) if a.name < b.name else (b.name, a.name)
                    if key in seen:
                        continue
                    seen.add(key)
                    pairs.append((a, b))
                    if len(pairs) >= budget:
                        return pairs
        return pairs

    def _clusters(
        self, candidates: list[MemoryNode], graph: MemoryGraph
    ) -> list[list[MemoryNode]]:
        """在「语义相似 + 共现」构成的邻接图上取连通分量。"""
        by_name = {n.name: n for n in candidates}
        adj: dict[str, set[str]] = {n.name: set() for n in candidates}
        for a, b in self._pairs(candidates):
            # 跨项目不聚类：把 A 项目的碎片和 B 项目的碎片沉淀成一条「结论」，
            # 等于制造一条不属于任何项目的记忆 —— 它会被两边都当成自己的。
            if a.scope != b.scope:
                continue
            if self._similarity(a, b, graph) >= self.config.consolidate_similarity:
                adj[a.name].add(b.name)
                adj[b.name].add(a.name)
        seen: set[str] = set()
        clusters: list[list[MemoryNode]] = []
        for name in adj:
            if name in seen:
                continue
            stack, component = [name], []
            seen.add(name)
            while stack:
                cur = stack.pop()
                component.append(by_name[cur])
                for nb in adj[cur]:
                    if nb not in seen:
                        seen.add(nb)
                        stack.append(nb)
            clusters.append(component)
        return clusters

    def _find_by_fingerprint(self, graph: MemoryGraph, fp: str) -> MemoryNode | None:
        for node in graph.all_nodes():
            if node.fingerprint == fp and node.tier in ("core", "fact"):
                return node
        return None

    def _semantically_compact(self, members: list[MemoryNode]) -> bool:
        """成员两两余弦都过门槛，才算「说同一件事」，防止一条链把无关内容串成一串。"""
        if self.vector_lookup is None or len(members) < 2:
            return False
        vectors = []
        for m in members:
            vec = self._vector(m)
            if not vec:
                return False
            vectors.append(vec)
        threshold = self.config.consolidate_similarity
        for i in range(len(vectors)):
            for j in range(i + 1, len(vectors)):
                if cosine(vectors[i], vectors[j]) < threshold:
                    return False
        return True

    def run(self, graph: MemoryGraph, now: float) -> ConsolidationResult:
        cfg = self.config
        self._vec_cache.clear()   # 每轮重取：语料与 IDF 可能已经变了
        candidates = [
            n
            for n in graph.all_nodes()
            if n.tier in ("ephemeral", "event")
            and not n.is_retired()
            and n.consolidated_into is None
            and not n.pinned
            and n.effective_weight(now) >= cfg.consolidate_min_weight
        ]
        # 候选封顶：只沉淀最强的那批。弱碎片本来就该先被遗忘，而不是先被聚类。
        if len(candidates) > cfg.consolidate_max_candidates:
            candidates.sort(key=lambda n: (-n.effective_weight(now), n.name))
            candidates = candidates[: cfg.consolidate_max_candidates]
        result = ConsolidationResult()
        clusters = self._clusters(candidates, graph)
        result.clusters_seen = len(clusters)

        for members in clusters:
            names = [m.name for m in members]
            shared = set.intersection(*[set(m.tags) for m in members]) if members else set()
            if len(members) < cfg.consolidate_min_group:
                result.clusters_skipped += 1
                continue
            # 共同标签是「同源」的证据；标签完全不同但内容同义，同样算同源。
            via = "tags"
            if len(shared) < cfg.consolidate_min_shared_tags:
                if self._semantically_compact(members):
                    via = "semantics"
                else:
                    result.clusters_skipped += 1
                    continue

            fp = self._fingerprint(names)
            if via == "semantics":
                result.by_semantics += 1
            else:
                result.by_tags += 1
            existing = self._find_by_fingerprint(graph, fp)
            if existing is not None:
                target = existing
                target.base_weight = min(MemoryNode.MAX_W, target.base_weight + 0.2)
                result.reused.append(target.name)
            else:
                all_tags = sorted(set().union(*[set(m.tags) for m in members]))
                target = MemoryNode(
                    name=f"core:{fp[2:10]}",
                    content=self.summarizer(members),
                    tier="core",
                    tags=tuple(all_tags),
                    created_at=now,
                    last_accessed=now,
                    base_weight=min(MemoryNode.MAX_W, 1.0 + 0.3 * len(members)),
                    access_count=max(1, round(sum(m.access_count for m in members) / len(members))),
                    origin="consolidated",
                    consolidated_from=tuple(names),
                    fingerprint=fp,
                    # 结论继承碎片的作用域（同组必然同域），否则它会飘成"全局"
                    scope=members[0].scope,
                )
                graph.add_node(target)
                result.created.append(target.name)

            for m in members:
                m.consolidated_into = target.name
                m.tier = "trace"
                graph.link(target.name, m.name, now, gain=2.0)

            result.events.append(
                Event(
                    ts=now,
                    kind="consolidate",
                    payload={
                        "target": target.name,
                        "members": names,
                        "fingerprint": fp,
                        "reused": existing is not None,
                        "via": via,
                    },
                )
            )
        return result
