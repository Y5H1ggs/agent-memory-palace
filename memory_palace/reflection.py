"""reflection.py - 自我反思：回看系统自己的历史，量化「我这套策略还好使吗」。

关键约束：反思只产出 findings + suggestions，绝不自动修改配置。
让系统自己改自己的账本，比遗忘失控更危险。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from .core import DAY, Event, MemoryGraph, PalaceConfig
from .style import StyleEngine


@dataclass
class ReflectionReport:
    generated_at: float
    window_days: float
    metrics: dict[str, float] = field(default_factory=dict)
    findings: list[str] = field(default_factory=list)
    suggestions: list[dict[str, Any]] = field(default_factory=list)
    narrative: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ReflectionEngine:
    """指标化回看。

    默认是**规则化监控**（阈值 → finding），不是 LLM 反思 —— 这两件事名字像，
    能力差很远，过去我在文档里没把这条界线说清楚，是表述问题。

    真正的"发现未知问题"需要能读事件序列并自己提假设，所以留了一个钩子：
    `reflector(payload) -> list[str]`。接一次 LLM 就能从"阈值告警"升级成"事后复盘"，
    代价是引入模型依赖 —— 因此它是**可选项**，默认关闭，且只产出文字，不碰配置。
    """

    def __init__(
        self,
        config: PalaceConfig,
        reflector: Callable[[dict[str, Any]], list[str]] | None = None,
    ) -> None:
        self.config = config
        self.reflector = reflector
        self.reflector_error: str | None = None

    @staticmethod
    def _num(value: Any, default: float = 0.0) -> float:
        """还没讲过一次自述时，这些指标是 None —— 不能 float(None) 炸掉整轮反思。"""
        return default if value is None else float(value)

    def run(
        self,
        graph: MemoryGraph,
        style: StyleEngine,
        events: list[Event],
        tombstone: dict[str, Any],
        now: float,
        context: dict[str, Any] | None = None,
    ) -> ReflectionReport:
        cfg = self.config
        start = now - cfg.reflection_window_days * DAY
        context = context or {}

        # 一次遍历同时做「窗口过滤」与「统计」。
        # 原来是：先建窗口列表，再用 count(kind) 扫 6 遍、payload 求和再扫 4 遍。
        # 5000 条事件时这些加起来只有 ~2ms，但它是每轮 cycle 都付的常数，
        # 而且写法本身在暗示「遍历很便宜」—— 不便宜，只是还没到痛的时候。
        counts: dict[str, int] = {}
        total_hits = core_hits = semantic_recalls = 0
        semantic_only_total = gc_deletes = 0
        scores: list[float] = []
        window: list[Event] = []
        for event in events:
            if event.ts < start:
                continue
            window.append(event)
            kind = event.kind
            counts[kind] = counts.get(kind, 0) + 1
            if kind == "recall":
                payload = event.payload
                total_hits += len(payload.get("names", ()))
                core_hits += len(payload.get("core_hits", ()))
                semantic_only_total += len(payload.get("semantic_only", ()))
                if payload.get("retrieval") == "semantic":
                    semantic_recalls += 1
                if "top_score" in payload:
                    scores.append(float(payload["top_score"]))
            elif kind == "gc":
                gc_deletes += len(event.payload.get("deleted", ()))

        recalls = counts.get("recall", 0)
        misses = counts.get("miss", 0)
        attempts = recalls + misses
        # 一次都没检索过 ≠ 覆盖率 0%。没有证据时说「未知」而不是「很窄」，
        # 否则刚建的库第一轮就会去下调语义门槛 —— 为了一个不存在的症状改参数。
        retrieval_coverage = recalls / attempts if attempts else 1.0
        consolidations = counts.get("consolidate", 0)
        crystallizations = counts.get("crystallize", 0)
        decrystallizations = counts.get("decrystallize", 0)
        restores = counts.get("restore", 0)
        reflections = counts.get("reflect", 0)
        semantic_only = semantic_only_total

        nodes = graph.all_nodes()
        core_nodes = [n for n in nodes if n.tier in ("core", "fact")]
        loose = [
            n
            for n in nodes
            if n.tier in ("ephemeral", "event") and n.consolidated_into is None
        ]
        hl = cfg.style_half_life_days
        confidences = [r.confidence(now, hl) for r in style.rules.values()]
        struct = context.get("structure") or {}
        grabbed_share = 1.0 - float(context.get("ungrabbed_share", 0.0))

        metrics = {
            "events_in_window": float(len(window)),
            "retrieval_coverage": round(retrieval_coverage, 4),
            "retrieval_attempts": float(attempts),
            "consolidation_yield": round(len(core_nodes) / max(1, len(nodes)), 4),
            "core_hit_ratio": round(core_hits / max(1, total_hits), 4),
            "forget_pressure": round(gc_deletes / max(1, len(nodes) + gc_deletes), 4),
            "gc_deletes": float(gc_deletes),
            "consolidations": float(consolidations),
            "crystallizations": float(crystallizations),
            "decrystallizations": float(decrystallizations),
            "restores": float(restores),
            "style_conflict_pairs": float(len(style.conflicts(now))),
            "active_style_conflicts": float(len(style.active_conflicts(now))),
            "unresolved_style_conflicts": float(len(style.pending_conflicts(now))),
            "avg_style_confidence": round(
                sum(confidences) / max(1, len(confidences)), 4
            ),
            "loose_fragments": float(len(loose)),
            "tombstone_size": float(len(tombstone)),
            "semantic_recalls": float(semantic_recalls),
            "semantic_only_hits": float(semantic_only),
            "avg_top_score": round(sum(scores) / max(1, len(scores)), 4),
            "logic_edges": float(context.get("logic_edges", 0)),
            "contested": float(context.get("contested", 0)),
            "entropy": float(context.get("entropy", 0.0)),
            "attention_top_share": float(context.get("top_share", 0.0)),
            "attention_top3_share": float(context.get("top3_share", 0.0)),
            # —— 结构熵：价值分布之外，图有没有形状 ——
            "structure_isolated": float(struct.get("isolated_share", 0.0)),
            "structure_fragmentation": float(struct.get("fragmentation", 0.0)),
            "structure_degree_entropy": float(struct.get("degree_entropy", 0.0)),
            "structure_giant_share": float(struct.get("giant_share", 0.0)),
            "grasp_coverage": round(grabbed_share, 4),
            "ungrabbed_share": float(context.get("ungrabbed_share", 0.0)),
            "narrative_coherence": self._num(context.get("narrative_coherence")),
            "narrative_reliability": self._num(context.get("narrative_reliability")),
            "narrative_monopoly": self._num(context.get("narrative_monopoly")),
            "foundation_version": float(context.get("foundation_version", 0)),
            "persona_drift": float((context.get("persona_drift") or {}).get("distance", 0.0)),
            "persona_oversell": float(
                (context.get("persona_consistency") or {}).get("oversell", 0.0)
            ),
            "persona_switches": float(
                (context.get("persona_churn") or {}).get("switches", 0)
            ),
            "persona_unjustified": float(
                (context.get("persona_churn") or {}).get("unjustified", 0)
            ),
        }

        findings: list[str] = []
        suggestions: list[dict[str, Any]] = []

        if metrics["retrieval_coverage"] < 0.5:
            findings.append(
                f"检索覆盖率 {metrics['retrieval_coverage']:.0%} 偏低，召回路径可能过窄或记忆已大面积遗忘。"
            )
        if consolidations == 0 and len(loose) >= cfg.consolidate_min_group:
            findings.append(
                f"仍有 {len(loose)} 条未沉淀碎片但本窗口零沉淀，聚类阈值可能过严。"
            )
            suggestions.append(
                {
                    "param": "consolidate_similarity",
                    "current": cfg.consolidate_similarity,
                    "suggested": round(max(0.2, cfg.consolidate_similarity - 0.05), 3),
                    "reason": "放宽语义相似度门槛，让相近碎片有机会聚类。",
                }
            )
        if core_nodes and metrics["core_hit_ratio"] < 0.05:
            findings.append(
                f"高层记忆已存在但检索命中占比仅 {metrics['core_hit_ratio']:.0%}，沉淀条件与真实使用脱节。"
            )
            suggestions.append(
                {
                    "param": "consolidate_min_weight",
                    "current": cfg.consolidate_min_weight,
                    "suggested": round(cfg.consolidate_min_weight + 0.1, 3),
                    "reason": "只允许更强的碎片沉淀，减少无人问津的摘要。",
                }
            )
        if restores > 0:
            findings.append(
                f"出现 {restores} 次误删恢复，遗忘阈值偏激进或 GC 周期过密。"
            )
        if metrics["forget_pressure"] > 0.3:
            findings.append(
                f"遗忘压力 {metrics['forget_pressure']:.0%} 过高，单轮清理占节点总量比重偏大。"
            )
            suggestions.append(
                {
                    "param": "forget_w",
                    "current": cfg.forget_w,
                    "suggested": round(max(0.005, cfg.forget_w / 2), 4),
                    "reason": "降低遗忘阈值，让弱记忆多留一个观察窗口。",
                }
            )
        if decrystallizations > 0:
            findings.append(
                f"有 {decrystallizations} 条风格被反证撤销，证据来源质量或观察期长度需要复核。"
            )
            suggestions.append(
                {
                    "param": "style_min_span_days",
                    "current": cfg.style_min_span_days,
                    "suggested": round(cfg.style_min_span_days + 1.0, 2),
                    "reason": "拉长固化前的观察跨度，过滤一时兴起。",
                }
            )
        if metrics["active_style_conflicts"] > 0:
            pairs = ", ".join(f"{a}↔{b}" for a, b in style.active_conflicts(now))
            findings.append(f"存在同时生效的对立风格条目：{pairs}（裁决器失效）。")
            suggestions.append(
                {
                    "param": "style_crystallize_confidence",
                    "current": cfg.style_crystallize_confidence,
                    "suggested": round(min(0.95, cfg.style_crystallize_confidence + 0.05), 3),
                    "reason": "提高固化门槛，减少互相矛盾的条目同时生效。",
                }
            )
        pending = style.pending_conflicts(now)
        if pending:
            names = ", ".join(
                f"{p['key']}({p['reason']})" for p in pending
            )
            findings.append(
                f"{len(pending)} 条风格因对立证据不足被停用，未参与输出：{names} —— 需要更多证据或人工裁决。"
            )
        if semantic_recalls >= 5 and semantic_only == 0:
            findings.append(
                f"{semantic_recalls} 次语义检索里没有一次召回标签不重叠的记忆，检索可能退化成了标签匹配。"
            )
        if semantic_recalls and metrics["avg_top_score"] < cfg.semantic_min_score + 0.05:
            findings.append(
                f"语义检索最高分均值仅 {metrics['avg_top_score']:.2f}，接近门槛 {cfg.semantic_min_score:.2f}，"
                "概念词典可能覆盖不足。"
            )
            suggestions.append(
                {
                    "param": "semantic_min_score",
                    "current": cfg.semantic_min_score,
                    "suggested": round(max(0.02, cfg.semantic_min_score - 0.02), 3),
                    "reason": "下调语义门槛，或补充概念词典后再收紧。",
                }
            )
        if "entropy_health" in context:
            health = context["entropy_health"]
            if health == "flat":
                findings.append(
                    f"重要性分布几乎无梯度（前三条合计只占 "
                    f"{metrics['attention_top3_share']:.0%}），什么都一样重要，等于没有取舍。"
                )
                suggestions.append(
                    {
                        "param": "importance_w_support",
                        "current": cfg.importance_w_support,
                        "suggested": round(cfg.importance_w_support + 0.1, 3),
                        "reason": "让有凭据的记忆在价值判断里拉开差距，避免一切等价。",
                    }
                )
            elif health == "narrow":
                findings.append(
                    f"重心压在一件事上（单项占 {metrics['attention_top_share']:.0%}），"
                    "自我正在收窄，分辨力在下降。"
                )
        if metrics["contested"] > 0:
            findings.append(
                f"{int(metrics['contested'])} 条记忆处于逻辑争议中，已标记、未裁决 —— 机器无权替你决定哪条为真。"
            )
        # —— 结构读数：价值可以健康，形状却是一盘散沙 ——
        struct_health = context.get("structure_health")
        if len(nodes) >= 5 and struct_health in ("isolated", "fragmented", "hub"):
            if struct_health == "isolated":
                findings.append(
                    f"{metrics['structure_isolated']:.0%} 的记忆一根边都没有：它们是孤岛，"
                    "价值分布可能看着健康，但彼此之间没有任何可推的路径。"
                )
            elif struct_health == "fragmented":
                findings.append(
                    f"记忆碎成 {int(struct.get('components', 0))} 个互不相连的块"
                    f"（最大一块只占 {metrics['structure_giant_share']:.0%}）："
                    "这不是「主题丰富」，是彼此推不出对方。"
                )
            else:
                findings.append(
                    f"边几乎全集中在一个中心（度分布熵 {metrics['structure_degree_entropy']:.2f}）："
                    "一个枢纽撑着整张图，拆了它就什么都没有。"
                )
        if len(nodes) >= 5 and metrics["ungrabbed_share"] > 0.85:
            findings.append(
                f"本轮只有 {metrics['grasp_coverage']:.0%} 的候选记忆被抓住："
                "预算相对存量已经太薄，剩下的大多只能靠自己撑着。"
            )
        monopoly = context.get("narrative_monopoly")
        if monopoly is not None and monopoly > cfg.narrative_monopoly_warn:
            findings.append(
                f"自述的单主题占比 {monopoly:.0%} 超过阈值 {cfg.narrative_monopoly_warn:.0%}，"
                "故事正在被一个主题垄断 —— 它越讲越顺，也越讲越窄。"
            )
        narr_rel = context.get("narrative_reliability")
        if narr_rel is not None and 0 < narr_rel < cfg.narrative_reliability_warn:
            findings.append(
                f"自述整体可靠度 {narr_rel:.0%} 偏低，这一版可能把当时的感受当成了当时的事实。"
            )

        # —— 人格层：漂移、人设一致性、切换失稳 ——
        drift = context.get("persona_drift") or {}
        health = drift.get("health")
        if health == "ruptured":
            findings.append(
                f"人格出现突变：{cfg.persona_velocity_window_days:.0f} 天内最快维度日变化 "
                f"{drift.get('peak_velocity', 0):.3f}，超过 {cfg.persona_velocity_warn:.3f}。"
                "无论离基座多远，这种速度都是断裂而不是成长。"
            )
        elif health == "drifting":
            findings.append(
                f"人格已明显偏离基础人格（距离 {drift.get('distance', 0):.2f}，"
                f"最大维度 {drift.get('peak_delta', 0):.2f}）—— 慢性偏移，需人裁决是修宪还是纠偏。"
            )
        elif health == "evolving":
            findings.append(
                f"人格在基座包络内演化中（距离 {drift.get('distance', 0):.2f}），属预期区间。"
            )
        clamped = drift.get("clamped") or {}
        if clamped:
            names = "、".join(f"{k}×{v}" for k, v in clamped.items())
            findings.append(
                f"有维度被基座硬包络挡回 {sum(clamped.values())} 次（{names}）—— "
                "这是基座在起作用，不是故障；若长期顶格，说明该维度该修宪而不是继续喂证据。"
            )
        consistency = context.get("persona_consistency") or {}
        if consistency and not consistency.get("consistent", True):
            findings.append(
                f"当前人设要求的表达超出人格支撑 {consistency.get('oversell', 0):.2f}"
                f"（容差 {consistency.get('tolerance', 0):.2f}），在演一个还不存在的自己："
                f"{'、'.join(consistency.get('underpowered', []))}。"
            )
        unreachable = consistency.get("unreachable") or {}
        if unreachable:
            findings.append(
                "人设要求了基座封顶之上的强度："
                + "、".join(f"{k}(+{v:.2f})" for k, v in unreachable.items())
                + " —— 这不是练得不够，是这条路被基座堵了。"
            )
        churn = context.get("persona_churn") or {}
        if churn.get("unstable"):
            findings.append(
                f"人设切换失稳：窗口内 {churn.get('switches')} 次、每小时 "
                f"{churn.get('per_hour')} 次，其中无理由 {churn.get('unjustified')} 次。"
                "有理由的切换是适应场合，无理由的频繁切换是不知道自己是谁。"
            )
        # 外部反思（可选）：只追加观察，不改 suggestions、不碰配置。
        # 失败绝不能炸穿一轮代谢 —— 与 autosave 同一原则：外部依赖坏了，主流程照跑，
        # 但要留下可观测的痕迹（reflector_error 进 snapshot）。
        self.reflector_error = None
        if self.reflector is not None:
            # 证据要给到**内容**，不能只给条数：只给「你有 47 条记忆」的模型
            # 除了复述数字什么也做不了，那种反思必然是空话。
            sample = sorted(
                nodes,
                key=lambda n: (-(n.narrative_heat or 0.0), -n.effective_weight(now)),
            )[:12]
            payload = {
                "metrics": dict(metrics),
                "recent_events": [e.to_dict() for e in window[-40:]],
                "nodes": len(nodes),
                "sample_nodes": [
                    {
                        "tier": n.tier,
                        "scope": n.scope,
                        "kind": n.kind,
                        "tags": list(n.tags),
                        "content": n.content,
                    }
                    for n in sample
                ],
                "narrative": {
                    "coherence": context.get("narrative_coherence"),
                    "reliability": context.get("narrative_reliability"),
                    "monopoly": context.get("narrative_monopoly"),
                },
            }
            try:
                produced = self.reflector(payload) or []
                for item in produced:
                    text = str(item).strip()
                    if text:
                        findings.append(f"〔外部反思〕{text}")
            except Exception as exc:  # noqa: BLE001
                self.reflector_error = f"{type(exc).__name__}: {exc}"

        if not findings:
            findings.append("本轮无异常信号：召回、沉淀、遗忘与风格演化均在预期区间。")
        narrative = (
            f"第 {reflections + 1} 轮自我回看：窗口 {cfg.reflection_window_days:.0f} 天内 "
            f"{len(window)} 次事件，沉淀 {consolidations} 次、遗忘 {gc_deletes} 条、"
            f"风格固化 {crystallizations} 次 / 撤销 {decrystallizations} 次；"
            f"当前 {len(nodes)} 个节点中 {len(core_nodes)} 个为高层记忆。"
            f"发现 {len(findings)} 项待核查信号，提出 {len(suggestions)} 条参数建议（不自动执行）。"
        )

        return ReflectionReport(
            generated_at=now,
            window_days=cfg.reflection_window_days,
            metrics=metrics,
            findings=findings,
            suggestions=suggestions,
            narrative=narrative,
        )
