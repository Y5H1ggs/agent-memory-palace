"""logic.py - 逻辑关联：不是「一起被想起过」，而是「从它能推出什么」。

共现边回答的是统计学问题（这两条常同时出现），语义余弦回答的是相似性问题
（这两条说的是同一类事）。两者都不是逻辑：
  · A 与 B 都是仓位话题     → 相似，但推不出彼此
  · A「单笔不超 1/3」+ B「所以最多买 3 只」→ B 从 A 得来，这才叫关联

所以这里单独一层：有向、有类型的边，可传递、可矛盾、可算「支撑度」。
检索时用扩散激活（Collins & Loftus）：想起 A，顺着逻辑走两跳，扇出越大分得越少。
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Iterable


class Relation(str, Enum):
    CAUSAL = "causal"          # A 导致 B
    ENTAILS = "entails"        # A 推出 B
    CONTRADICTS = "contradicts"  # A 与 B 不能同时为真
    INSTANCE_OF = "instance_of"  # A 是 B 的一个实例
    BEFORE = "before"          # A 先于 B（时序，不传递强度）


# 哪些关系能把激活传下去。矛盾不传激活，只报警。
TRANSMISSIVE: frozenset[str] = frozenset(
    {Relation.CAUSAL.value, Relation.ENTAILS.value, Relation.INSTANCE_OF.value}
)
# 哪些关系构成「凭据」（孤证不立靠这一项）
SUPPORTING: frozenset[str] = frozenset({Relation.CAUSAL.value, Relation.ENTAILS.value})


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


@dataclass
class LogicEdge:
    src: str
    dst: str
    kind: str
    strength: float = 1.0
    since: float = 0.0
    observations: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "LogicEdge":
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        edge = cls(**known)
        if edge.kind not in {r.value for r in Relation}:
            raise ValueError(f"未知关系类型: {edge.kind!r}")
        if not 0.0 < edge.strength <= 1.0:
            raise ValueError(f"关系强度必须落在 (0, 1]，实际 {edge.strength}")
        return edge


class LogicGraph:
    """有向、带类型的关系层。与共现图并列，互不覆盖：一条边可以说「A 推出 B」，
    同时另一层记录「A 与 B 常被一起检索」。两层都被保留，因为它们是两回事。"""

    def __init__(self) -> None:
        self._edges: dict[tuple[str, str, str], LogicEdge] = {}
        self._out: dict[str, dict[tuple[str, str, str], LogicEdge]] = {}
        self._in: dict[str, dict[tuple[str, str, str], LogicEdge]] = {}

    def __len__(self) -> int:
        return len(self._edges)

    def edges(self) -> list[LogicEdge]:
        return list(self._edges.values())

    def assert_relation(
        self, src: str, dst: str, kind: str | Relation, strength: float = 1.0, now: float = 0.0
    ) -> LogicEdge:
        kind_value = kind.value if isinstance(kind, Relation) else str(kind)
        if kind_value not in {r.value for r in Relation}:
            raise ValueError(f"未知关系类型: {kind_value!r}")
        if src == dst:
            raise ValueError(f"关系不得自指: {src}")
        if not 0.0 < strength <= 1.0:
            raise ValueError(f"关系强度必须落在 (0, 1]，实际 {strength}")
        key = (src, dst, kind_value)
        edge = self._edges.get(key)
        if edge is None:
            edge = LogicEdge(src=src, dst=dst, kind=kind_value, strength=strength, since=now)
            self._edges[key] = edge
            self._out.setdefault(src, {})[key] = edge
            self._in.setdefault(dst, {})[key] = edge
        else:
            # 同一断言重复出现：强度软饱和上升，观察次数单独记账（可审计的「说过几次」）
            edge.strength = _clamp(edge.strength + (1.0 - edge.strength) * 0.25 * strength, 0.0, 1.0)
            edge.observations += 1
        return edge

    def outgoing(self, name: str, kinds: Iterable[str] | None = None) -> list[LogicEdge]:
        allowed = {k.value if isinstance(k, Relation) else k for k in kinds} if kinds else None
        return [
            e
            for e in self._out.get(name, {}).values()
            if allowed is None or e.kind in allowed
        ]

    def incoming(self, name: str, kinds: Iterable[str] | None = None) -> list[LogicEdge]:
        allowed = {k.value if isinstance(k, Relation) else k for k in kinds} if kinds else None
        return [
            e
            for e in self._in.get(name, {}).values()
            if allowed is None or e.kind in allowed
        ]

    # ---------- 支撑度：孤证不立 ----------

    def support(self, name: str, cap: float = 1.0) -> float:
        """一条记忆的凭据强度。

        被别人推出（incoming）是最硬的凭据；能推出别人（outgoing）也算承重，
        但打折 —— 「很多结论都靠它」和「它有依据」不是同一件事。
        """
        incoming = sum(e.strength for e in self.incoming(name, SUPPORTING))
        outgoing = sum(e.strength for e in self.outgoing(name, SUPPORTING))
        return _clamp(incoming + 0.3 * outgoing, 0.0, cap)

    def support_map(self) -> dict[str, float]:
        names: set[str] = set()
        for src, dst, _ in self._edges:
            names.add(src)
            names.add(dst)
        return {name: self.support(name) for name in names}

    # ---------- 传递：A→B→C ----------

    def closure(
        self,
        name: str,
        kinds: Iterable[str] = (Relation.ENTAILS, Relation.CAUSAL),
        max_hops: int = 3,
        hop_decay: float = 0.6,
    ) -> dict[str, float]:
        """沿可传递关系展开，返回 {目标: 路径强度}。取最强路径，不累加环。"""
        frontier = {name: 1.0}
        seen = {name}
        result: dict[str, float] = {}
        for _ in range(max(0, max_hops)):
            nxt: dict[str, float] = {}
            for node, act in frontier.items():
                for edge in self.outgoing(node, kinds):
                    if edge.dst in seen:
                        continue
                    gain = act * hop_decay * edge.strength
                    if gain > nxt.get(edge.dst, 0.0):
                        nxt[edge.dst] = gain
            if not nxt:
                break
            seen.update(nxt)
            result.update(nxt)
            frontier = nxt
        return result

    # ---------- 矛盾：直接 + 一跳传播 ----------

    def contradiction_pairs(self) -> list[tuple[str, str, str, float]]:
        """返回 (a, b, 来源, 强度)。来源 direct / propagated。"""
        found: list[tuple[str, str, str, float]] = []
        seen: set[frozenset[str]] = set()
        for edge in self._edges.values():
            if edge.kind != Relation.CONTRADICTS.value:
                continue
            key = frozenset((edge.src, edge.dst))
            if key in seen:
                continue
            seen.add(key)
            found.append((edge.src, edge.dst, "direct", edge.strength))
        # 传播：若 A 推出 B，而 A 与 C 矛盾，则 B 与 C 也（弱）矛盾。
        # 这是相似度模型永远做不到的：矛盾会顺着推理链走。
        for edge in self._edges.values():
            if edge.kind not in SUPPORTING:
                continue
            for contra in self.outgoing(edge.src, [Relation.CONTRADICTS]):
                key = frozenset((edge.dst, contra.dst))
                if key in seen or edge.dst == contra.dst:
                    continue
                seen.add(key)
                found.append(
                    (edge.dst, contra.dst, "propagated", edge.strength * contra.strength * 0.5)
                )
        return found

    def contested(self) -> dict[str, float]:
        """被判为「有争议」的节点及其争议强度。系统只标记，不擅自裁决。"""
        out: dict[str, float] = {}
        for a, b, _source, strength in self.contradiction_pairs():
            out[a] = max(out.get(a, 0.0), strength)
            out[b] = max(out.get(b, 0.0), strength)
        return out

    # ---------- 扩散激活 ----------

    def spread(
        self,
        seeds: Iterable[str],
        max_hops: int = 2,
        hop_decay: float = 0.55,
        fanout_penalty: bool = True,
    ) -> dict[str, float]:
        """想起一件事，顺着逻辑想起另一件。

        扇出惩罚：一个节点连的东西越多，每条边分到的激活越少 —— 否则「什么都连着」
        的枢纽节点会垄断所有联想，那就不叫联想，叫回音室。
        """
        seed_set = {s for s in seeds}
        result: dict[str, float] = {}
        frontier = {s: 1.0 for s in seed_set}
        for _ in range(max(0, max_hops)):
            nxt: dict[str, float] = {}
            for node, act in frontier.items():
                outs = [e for e in self.outgoing(node, TRANSMISSIVE)]
                if not outs:
                    continue
                penalty = 1.0 / math.sqrt(len(outs)) if fanout_penalty else 1.0
                for edge in outs:
                    gain = act * hop_decay * edge.strength * penalty
                    if gain > nxt.get(edge.dst, 0.0):
                        nxt[edge.dst] = gain
            nxt = {k: v for k, v in nxt.items() if k not in seed_set}
            if not nxt:
                break
            for name, act in nxt.items():
                if act > result.get(name, 0.0):
                    result[name] = act
            frontier = nxt
        return result

    # ---------- 维护与持久化 ----------

    def prune(self, names: Iterable[str]) -> list[tuple[str, str, str]]:
        """节点被遗忘时，它的关系边必须一起消失，否则就是悬空断言。"""
        dead = set(names)
        removed: list[tuple[str, str, str]] = []
        for key in list(self._edges):
            src, dst, kind = key
            if src in dead or dst in dead:
                edge = self._edges.pop(key)
                self._out.get(src, {}).pop(key, None)
                self._in.get(dst, {}).pop(key, None)
                removed.append((edge.src, edge.dst, edge.kind))
        return removed

    def ghosts(self, valid: Iterable[str]) -> list[tuple[str, str, str]]:
        allowed = set(valid)
        return sorted(
            (src, dst, kind)
            for src, dst, kind in self._edges
            if src not in allowed or dst not in allowed
        )

    def to_dict(self) -> dict[str, Any]:
        return {"edges": [e.to_dict() for e in self._edges.values()]}

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None, valid: Iterable[str] | None = None) -> "LogicGraph":
        g = cls()
        allowed = set(valid) if valid is not None else None
        for raw in (d or {}).get("edges", []):
            edge = LogicEdge.from_dict(raw)
            if allowed is not None and (edge.src not in allowed or edge.dst not in allowed):
                raise ValueError(f"快照含悬空关系: {edge.src} -{edge.kind}-> {edge.dst}")
            key = (edge.src, edge.dst, edge.kind)
            g._edges[key] = edge
            g._out.setdefault(edge.src, {})[key] = edge
            g._in.setdefault(edge.dst, {})[key] = edge
        return g
