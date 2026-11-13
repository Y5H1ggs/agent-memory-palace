"""narrative.py - 自我叙事：不是把所有记忆倒出来，而是「挑几条、排个序、讲成一个故事」。

这个模块是对上一版的一次纠错。上一版把「自我」写成一个叫 self:reflection 的节点 ——
那就等于说「我」是记忆里的一条事实：会衰减、会被检索强化、会被 GC 扫走。
可用户的原话是「由我选择记住什么、遗忘什么、以及如何讲述这个故事来定义」——
自我是**读图的方式**，不是图里的一个条目。

于是：
  · Narrative 是投影（projection），不是节点；它有自己的版本号，不进共现图。
  · 被反复讲述的条目获得叙事地位（narrative_hits）→ 价值上升 → 更难被遗忘。
    这是「我怎么讲这个故事」反过来塑造「我记得住什么」，一条闭环。
  · 闭环必须能被观测到收窄：单主题占比过高就是回音室，要喊出来，不能自我陶醉。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from .core import MemoryGraph


@dataclass
class Chapter:
    title: str
    arc: str  # 起源 / 转折 / 当下
    nodes: list[str]
    share: float = 0.0
    reliability: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class NarrativeRevision:
    rev: int
    at: float
    chapters: list[Chapter] = field(default_factory=list)
    selected: list[str] = field(default_factory=list)
    dropped: list[dict[str, Any]] = field(default_factory=list)
    unreliable: list[str] = field(default_factory=list)
    coherence: float = 0.0
    reliability: float = 0.0
    monopoly: float = 0.0
    text: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "rev": self.rev,
            "at": self.at,
            "chapters": [c.to_dict() for c in self.chapters],
            "selected": list(self.selected),
            "dropped": list(self.dropped),
            "unreliable": list(self.unreliable),
            "coherence": self.coherence,
            "reliability": self.reliability,
            "monopoly": self.monopoly,
            "text": self.text,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "NarrativeRevision":
        return cls(
            rev=int(d["rev"]),
            at=float(d["at"]),
            chapters=[Chapter(**c) for c in d.get("chapters", [])],
            selected=list(d.get("selected", [])),
            dropped=list(d.get("dropped", [])),
            unreliable=list(d.get("unreliable", [])),
            coherence=float(d.get("coherence", 0.0)),
            reliability=float(d.get("reliability", 0.0)),
            monopoly=float(d.get("monopoly", 0.0)),
            text=str(d.get("text", "")),
        )


def _jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 1.0
    return len(sa & sb) / len(sa | sb)


class NarrativeEngine:
    def __init__(self, config: Any) -> None:
        self.config = config
        self.rev = 0
        self.history: list[NarrativeRevision] = []

    @property
    def head(self) -> NarrativeRevision | None:
        return self.history[-1] if self.history else None

    def compose(
        self,
        graph: MemoryGraph,
        assessment: dict[str, dict[str, float]],
        reliability: dict[str, float] | None = None,
        contested: Iterable[str] = (),
        now: float = 0.0,
    ) -> NarrativeRevision:
        cfg = self.config
        reliability = reliability or {}
        contested_set = set(contested)
        nodes = {n.name: n for n in graph.all_nodes()}
        # 故事讲结论，不讲底稿：已沉淀的碎片（trace / 已归属某条结晶）不进入自述候选。
        # 它们的存在是溯源用的，不是用来反复讲述的。
        accountable = {
            name: node
            for name, node in nodes.items()
            if not (node.tier == "trace" or node.consolidated_into) and name in assessment
        }
        ranked = sorted(
            accountable,
            key=lambda n: (-assessment[n]["total"], n),
        )

        top_total = assessment[ranked[0]]["total"] if ranked else 0.0
        floor = max(cfg.narrative_select_importance, cfg.narrative_relative_floor * top_total)
        selected = [name for name in ranked if assessment[name]["total"] >= floor][
            : cfg.narrative_max_nodes
        ]
        selected_set = set(selected)
        dropped = [
            {
                "name": name,
                "total": assessment[name]["total"],
                "reason": "capacity" if assessment[name]["total"] >= floor else "below_threshold",
            }
            for name in ranked
            if name not in selected_set
        ]
        # 底层碎片单独记账：它们不是「被筛掉」，而是压根不属于叙述层。
        dropped.extend(
            {
                "name": name,
                "total": assessment.get(name, {}).get("total", 0.0),
                "reason": "delegated_fragment",
            }
            for name in nodes
            if name not in accountable and name not in selected_set
        )

        total_mass = sum(assessment[n]["total"] for n in selected) or 1.0
        buckets: dict[str, list[str]] = {}
        for name in selected:
            node = nodes[name]
            title = node.tags[0] if node.tags else "无题"
            buckets.setdefault(title, []).append(name)

        # 章节容量有限：小主题合并为「其他」，否则自述会碎成一条条流水账。
        def mass_of(members: list[str]) -> float:
            return sum(assessment[m]["total"] for m in members) / total_mass

        if len(buckets) > cfg.narrative_max_chapters:
            ordered = sorted(buckets.items(), key=lambda kv: (-mass_of(kv[1]), kv[0]))
            keep = dict(ordered[: cfg.narrative_max_chapters - 1])
            merged: list[str] = []
            for _title, members in ordered[cfg.narrative_max_chapters - 1 :]:
                merged.extend(members)
            if merged:
                keep.setdefault("其他", []).extend(merged)
            buckets = keep

        chapters: list[Chapter] = []
        for title, members in buckets.items():
            mean_created = sum(nodes[m].created_at for m in members) / len(members)
            mean_accessed = sum(nodes[m].last_accessed for m in members) / len(members)
            chapters.append(
                Chapter(
                    title=title,
                    arc="",
                    nodes=sorted(members, key=lambda m: nodes[m].created_at),
                    share=round(mass_of(members), 6),
                    reliability=round(
                        sum(reliability.get(m, 1.0) for m in members) / len(members), 6
                    ),
                )
            )
        chapters.sort(key=lambda c: min(nodes[m].created_at for m in c.nodes))

        # 叙事弧：最早的章节是起源，此刻仍在被触碰的是当下，中间的是转折。
        if chapters:
            if len(chapters) == 1:
                chapters[0].arc = "当下"
            else:
                chapters[0].arc = "起源"
                rest = chapters[1:]
                current = max(
                    rest,
                    key=lambda c: sum(nodes[m].last_accessed for m in c.nodes) / len(c.nodes),
                )
                for chapter in rest:
                    chapter.arc = "当下" if chapter is current else "转折"
                chapters.sort(key=lambda c: ({"起源": 0, "转折": 1, "当下": 2}[c.arc], c.title))

        prev_selected = self.head.selected if self.head else selected
        continuity = _jaccard(selected, prev_selected) if self.head else 1.0
        contested_share = (
            len(selected_set & contested_set) / len(selected_set) if selected_set else 0.0
        )
        core_names = {n.name for n in graph.all_nodes() if n.tier in ("core", "fact")}
        core_coverage = (
            len(selected_set & core_names) / len(core_names) if core_names else 1.0
        )
        coherence = round(
            0.45 * continuity + 0.30 * (1.0 - contested_share) + 0.25 * core_coverage, 6
        )

        mean_reliability = (
            sum(reliability.get(n, 1.0) for n in selected) / len(selected)
            if selected
            else 0.0
        )
        monopoly = round(max((c.share for c in chapters), default=0.0), 6)

        self.rev += 1
        unreliable = [n for n in selected if reliability.get(n, 1.0) < cfg.narrative_reliability_warn]
        revision = NarrativeRevision(
            rev=self.rev,
            at=now,
            chapters=chapters,
            selected=sorted(selected, key=lambda n: (-assessment[n]["total"], n)),
            dropped=dropped,
            unreliable=sorted(unreliable),
            coherence=coherence,
            reliability=round(mean_reliability, 6),
            monopoly=monopoly,
            text="",
        )
        revision.text = self._render(revision, len(nodes))
        self.history.append(revision)
        if len(self.history) > 50:
            self.history = self.history[-50:]
        return revision

    def _render(self, revision: NarrativeRevision, total_nodes: int) -> str:
        cfg = self.config
        # 不可靠是**条目级**的，不是章节级：把某条模糊的记忆混在整章里平均掉，
        # 等于用「平均可靠」掩盖「某一句是假的」。
        arc = " → ".join(
            f"{c.arc}「{c.title}」{len(c.nodes)}条" for c in revision.chapters
        )
        head = (
            f"第 {revision.rev} 次自述：{total_nodes} 条记忆里，我留下 {len(revision.selected)} 条、"
            f"放下 {len(revision.dropped)} 条。"
        )
        body = f"线索是 {arc}。" if arc else "此刻无话可说。"
        metrics = (
            f"连续性 {revision.coherence:.2f}，可靠度 {revision.reliability:.2f}，"
            f"单主题占比 {revision.monopoly:.2f}。"
        )
        warnings_txt = ""
        if revision.unreliable:
            names = "、".join(revision.unreliable[:3])
            warnings_txt += (
                f"（{names} 这 {len(revision.unreliable)} 条的细节可能失真："
                "我记得最牢的部分，恰恰最可能是被情绪改写过的）"
            )
        if revision.reliability and revision.reliability < cfg.narrative_reliability_warn:
            warnings_txt += "（这一版整体建立在模糊的记忆上，我可能把当时的感受当成了当时的事实）"
        if revision.monopoly > cfg.narrative_monopoly_warn:
            warnings_txt += "（单一主题吃掉大半叙述，我的自述正在收窄）"
        return head + body + metrics + warnings_txt

    # ---------- 叙事地位 ----------

    def apply_centrality(
        self, graph: MemoryGraph, revision: NarrativeRevision, now: float | None = None
    ) -> list[str]:
        """被讲进故事里的条目获得叙事地位 —— 这是自我的反馈回路。

        注意这是**会退潮**的地位：热度按 `(1-heat)/cap` 增量上涨，并随时间衰减；
        没被讲到的条目不动，于是它们的相对地位会自然往下掉。
        只有累计次数、没有时间，等于讲过一次就发终身爵位。
        """
        cfg = self.config
        cap = max(1, cfg.narrative_centrality_cap)
        moment = revision.at if now is None else now
        touched: list[str] = []
        for name in revision.selected:
            node = graph.get(name)
            if node is not None:
                node.touch_narrative(moment, cap)
                touched.append(name)
        return touched

    def stability_of_story(self) -> float:
        """连续几次自述里，故事变了多少。太低=僵化，太高=没有自我。"""
        if len(self.history) < 2:
            return 1.0
        return round(
            sum(
                _jaccard(self.history[i].selected, self.history[i - 1].selected)
                for i in range(1, len(self.history))
            )
            / (len(self.history) - 1),
            6,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "rev": self.rev,
            "history": [r.to_dict() for r in self.history[-20:]],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None, config: Any) -> "NarrativeEngine":
        engine = cls(config)
        if not d:
            return engine
        engine.rev = int(d.get("rev", 0))
        engine.history = [NarrativeRevision.from_dict(r) for r in d.get("history", [])]
        return engine
