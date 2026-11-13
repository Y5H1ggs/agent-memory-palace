"""core.py - 记忆节点、共现图、幂等衰减、配置与原子持久化。

设计要点：权重不是被反复改写的余额，而是「锚点 + 时间」算出来的读数。
因此 GC 跑一次和跑一百次，账本完全一致。
"""
from __future__ import annotations

import json
import math
import os
import tempfile
import threading
import time
import warnings
from dataclasses import asdict, dataclass, field, fields as dc_fields
from typing import Any, Callable, ClassVar, Iterable

from .affect import Affect

DAY = 86400.0


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))

Clock = Callable[[], float]


def dataclass_field_names(cls: type) -> set[str]:
    """只取真实字段，排除 ClassVar（否则会把常量误当成待恢复的配置）。"""
    return {f.name for f in dc_fields(cls)}


def wall_clock() -> float:
    return time.time()


TIER_STABILITY: dict[str, float] = {
    "trace": 0.6,
    "ephemeral": 1.0,
    "event": 1.6,
    "fact": 3.0,
    "core": 6.0,
}


@dataclass
class PalaceConfig:
    """所有可调参数集中在此，反思引擎只提建议、不擅自改这里。"""

    forget_w: float = 0.05
    access_gain: float = 0.5
    consolidate_min_group: int = 3
    consolidate_min_shared_tags: int = 2
    consolidate_min_weight: float = 0.2
    consolidate_similarity: float = 0.5
    # 沉淀是维护动作，不能把一次 run_cycle 拖成秒级：候选封顶 + 大规模时分桶配对。
    consolidate_max_candidates: int = 600
    consolidate_block_threshold: int = 120
    consolidate_max_pairs: int = 60000
    style_crystallize_confidence: float = 0.70
    style_min_observations: int = 3
    style_min_span_days: float = 1.0
    style_decrystallize_confidence: float = 0.50
    style_half_life_days: float = 30.0
    reflection_window_days: float = 30.0
    max_events: int = 5000
    semantic_alpha: float = 1.0
    semantic_tag_beta: float = 0.6
    # 时近分量只用于排序：gamma 上限 0.1 < semantic_min_score，单靠「够新」不足以越过召回门槛。
    semantic_recency_gamma: float = 0.10
    semantic_min_score: float = 0.12
    semantic_relative_floor: float = 0.55
    # 多义词按上下文分配份额；floor 是「没有任何上下文时给每个候选概念的保底份额」，
    # 没有它，一个孤零零的「资金」会被判给得分更高的那个概念，等于回到硬编码。
    semantic_ambiguity_floor: float = 0.15
    # 从语料里长出新概念词：至少与同一概念的 2 个不同种子词同篇出现，
    # 且不能是那种满语料都是的高频词（df 份额上限），否则「记录」这类词会把所有主题连成一坨。
    semantic_learn_min_df_ratio: float = 0.12
    semantic_learn_max_df_ratio: float = 0.60
    semantic_learn_min_lift: float = 1.6
    # 纯净度：这个词出现的篇目里，有多少篇属于同一个概念。低于它说明是跨主题的泛用词。
    semantic_learn_min_purity: float = 0.75
    style_adjudication_margin: float = 0.05

    # —— 情感权重：潮水（唤醒）、河床（刻痕）、失真代价 ——
    affect_arousal_half_life_days: float = 3.0
    affect_valence_half_life_days: float = 10.0
    affect_stability_gain: float = 1.8
    affect_distortion: float = 0.35
    # —— 逻辑关联：传递、扩散、扇出惩罚 ——
    logic_hop_decay: float = 0.55
    logic_max_hops: int = 2
    logic_spread_threshold: float = 0.12
    # —— 选择：importance 四项权重 + 取舍阈值 + 代谢预算 ——
    importance_w_weight: float = 1.0
    importance_w_affect: float = 0.9
    importance_w_support: float = 0.8
    importance_w_central: float = 1.1
    importance_forget: float = 0.06
    grasp_budget: float = 3.0
    grasp_gain: float = 0.30
    # —— 自我叙事 ——
    narrative_max_nodes: int = 9
    narrative_max_chapters: int = 4
    narrative_select_importance: float = 0.12
    narrative_relative_floor: float = 0.25
    narrative_centrality_cap: int = 8
    # 叙事地位不是终身爵位：不被讲述就会退潮。这个半衰期决定「多久不讲就掉一半」。
    narrative_heat_half_life_days: float = 21.0
    narrative_monopoly_warn: float = 0.70
    narrative_reliability_warn: float = 0.70
    # —— 熵的健康区间：全平与过度集中都是自我的消失 ——
    entropy_flat_below: float = 0.45
    entropy_narrow_above: float = 0.60
    # —— 结构熵：价值分布之外，图本身有没有形状 ——
    structure_isolated_warn: float = 0.50
    structure_fragment_warn: float = 0.45
    structure_hub_below: float = 0.35
    # —— 人格：惯性、漂移、一致性、churn ——
    persona_learning_rate: float = 0.30
    persona_inertia: float = 0.80
    persona_recent_limit: int = 64
    persona_drift_warn: float = 0.18
    persona_drift_rupture: float = 0.35
    persona_peak_weight: float = 0.65
    persona_velocity_window_days: float = 3.0
    persona_velocity_warn: float = 0.06
    persona_consistency_tolerance: float = 0.30
    persona_churn_per_hour: float = 0.50
    persona_churn_min_switches: int = 3
    persona_history_limit: int = 200
    # —— 自修正：白名单外的参数一律不许碰 ——
    selfcorrect_max_step_ratio: float = 0.30
    selfcorrect_verify_hours: float = 24.0
    selfcorrect_max_per_cycle: int = 1
    selfcorrect_max_reverts: int = 2
    # 指标要「真的变好」才算数：变化幅度小于这个比例，视为噪声，不构成保留理由。
    selfcorrect_effect_min: float = 0.05
    # 观测样本比观察窗还旧时不许判 —— 这就是「用上一轮的指标判这一轮的修正」的漏洞。
    selfcorrect_max_extensions: int = 2
    selfcorrect_settle_ratio: float = 0.5

    INT_FIELDS: ClassVar[tuple[str, ...]] = (
        "consolidate_min_group",
        "consolidate_min_shared_tags",
        "consolidate_max_candidates",
        "consolidate_block_threshold",
        "consolidate_max_pairs",
        "style_min_observations",
        "max_events",
        "logic_max_hops",
        "narrative_max_nodes",
        "narrative_max_chapters",
        "narrative_centrality_cap",
        "persona_recent_limit",
        "persona_history_limit",
        "persona_churn_min_switches",
        "selfcorrect_max_per_cycle",
        "selfcorrect_max_reverts",
        "selfcorrect_max_extensions",
    )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def validate(self) -> None:
        """坏参数必须响亮地失败，不能安静地跑出一个错的系统。"""
        for key, value in self.to_dict().items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(
                    f"config.{key} 必须是数值，实际为 {type(value).__name__}: {value!r}"
                )
            if not math.isfinite(float(value)):
                raise ValueError(f"config.{key} 不是有限数: {value!r}")
        for key in self.INT_FIELDS:
            value = getattr(self, key)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"config.{key} 必须是整数，实际为 {value!r}")
        if not 0 < self.forget_w < 1:
            raise ValueError(f"config.forget_w 必须落在 (0, 1)，实际 {self.forget_w}")
        if self.access_gain <= 0:
            raise ValueError(f"config.access_gain 必须为正，实际 {self.access_gain}")
        if not 0 <= self.consolidate_similarity <= 1:
            raise ValueError("config.consolidate_similarity 必须落在 [0, 1]")
        if not 0 <= self.consolidate_min_weight <= MemoryNode.MAX_W:
            raise ValueError("config.consolidate_min_weight 超出权重值域")
        for key in ("style_crystallize_confidence", "style_decrystallize_confidence"):
            if not 0 <= getattr(self, key) <= 1:
                raise ValueError(f"config.{key} 必须落在 [0, 1]")
        if self.style_half_life_days <= 0 or self.reflection_window_days <= 0:
            raise ValueError("半衰期与反思窗口必须为正")
        if self.consolidate_min_group < 2 or self.consolidate_min_shared_tags < 0:
            raise ValueError("沉淀分组阈值非法")
        if self.consolidate_max_candidates < self.consolidate_min_group:
            raise ValueError("consolidate_max_candidates 不能小于分组下限")
        if self.consolidate_block_threshold < 2 or self.consolidate_max_pairs < 1:
            raise ValueError("沉淀分桶阈值与配对上限必须为正")
        if self.style_min_observations < 1 or self.max_events < 1:
            raise ValueError("证据数下限与事件上限非法")
        if self.style_min_span_days < 0:
            raise ValueError("风格观察跨度不能为负")
        for key in ("semantic_alpha", "semantic_tag_beta", "semantic_recency_gamma"):
            if getattr(self, key) < 0:
                raise ValueError(f"config.{key} 不能为负")
        if not 0 <= self.semantic_min_score <= 1:
            raise ValueError("config.semantic_min_score 必须落在 [0, 1]")
        if not 0 <= self.semantic_relative_floor <= 1:
            raise ValueError("config.semantic_relative_floor 必须落在 [0, 1]")
        if not 0 <= self.semantic_ambiguity_floor <= 1:
            raise ValueError("config.semantic_ambiguity_floor 必须落在 [0, 1]")
        if not 0 <= self.semantic_learn_min_df_ratio < self.semantic_learn_max_df_ratio <= 1:
            raise ValueError("语料学习频率下限必须严格小于上限，且都落在 [0, 1]")
        if self.semantic_learn_min_lift <= 1.0:
            raise ValueError("config.semantic_learn_min_lift 必须大于 1（否则学不到任何区分度）")
        if not 0.5 <= self.semantic_learn_min_purity <= 1:
            raise ValueError("config.semantic_learn_min_purity 必须落在 [0.5, 1]")
        if not 0 <= self.style_adjudication_margin <= 1:
            raise ValueError("config.style_adjudication_margin 必须落在 [0, 1]")
        # —— 情感 ——
        for key in ("affect_arousal_half_life_days", "affect_valence_half_life_days"):
            if getattr(self, key) <= 0:
                raise ValueError(f"config.{key} 必须为正")
        if self.affect_stability_gain < 0:
            raise ValueError("config.affect_stability_gain 不能为负")
        if not 0 <= self.affect_distortion <= 1:
            raise ValueError("config.affect_distortion 必须落在 [0, 1]")
        # —— 逻辑 ——
        if not 0 <= self.logic_hop_decay <= 1:
            raise ValueError("config.logic_hop_decay 必须落在 [0, 1]")
        if self.logic_max_hops < 0 or self.logic_max_hops > 8:
            raise ValueError("config.logic_max_hops 必须落在 [0, 8]")
        if not 0 <= self.logic_spread_threshold <= 1:
            raise ValueError("config.logic_spread_threshold 必须落在 [0, 1]")
        # —— 选择 ——
        for key in (
            "importance_w_weight",
            "importance_w_affect",
            "importance_w_support",
            "importance_w_central",
        ):
            if getattr(self, key) < 0:
                raise ValueError(f"config.{key} 不能为负")
        if (
            self.importance_w_weight
            + self.importance_w_affect
            + self.importance_w_support
            + self.importance_w_central
            <= 0
        ):
            raise ValueError("importance 四项权重之和必须为正，否则价值判断退化")
        if not 0 < self.importance_forget < 1:
            raise ValueError("config.importance_forget 必须落在 (0, 1)")
        if self.grasp_budget < 0:
            raise ValueError("config.grasp_budget 不能为负（0 表示本轮一条也不抓）")
        if self.grasp_gain <= 0:
            raise ValueError("config.grasp_gain 必须为正")
        # —— 叙事 ——
        if self.narrative_max_nodes < 1:
            raise ValueError("config.narrative_max_nodes 至少为 1")
        if self.narrative_max_chapters < 1:
            raise ValueError("config.narrative_max_chapters 至少为 1")
        if not 0 < self.narrative_select_importance < 1:
            raise ValueError("config.narrative_select_importance 必须落在 (0, 1)")
        if not 0 <= self.narrative_relative_floor <= 1:
            raise ValueError("config.narrative_relative_floor 必须落在 [0, 1]")
        if self.narrative_centrality_cap < 1:
            raise ValueError("config.narrative_centrality_cap 至少为 1")
        if self.narrative_heat_half_life_days <= 0:
            raise ValueError("config.narrative_heat_half_life_days 必须为正")
        for key in ("narrative_monopoly_warn", "narrative_reliability_warn"):
            if not 0 <= getattr(self, key) <= 1:
                raise ValueError(f"config.{key} 必须落在 [0, 1]")
        # —— 熵 ——
        for key in ("entropy_flat_below", "entropy_narrow_above"):
            if not 0 <= getattr(self, key) <= 1:
                raise ValueError(f"config.{key} 必须落在 [0, 1]")
        for key in (
            "structure_isolated_warn",
            "structure_fragment_warn",
            "structure_hub_below",
        ):
            if not 0 <= getattr(self, key) <= 1:
                raise ValueError(f"config.{key} 必须落在 [0, 1]")
        # —— 人格 ——
        if not 0 < self.persona_learning_rate <= 1:
            raise ValueError("config.persona_learning_rate 必须落在 (0, 1]")
        if self.persona_inertia < 0:
            raise ValueError("config.persona_inertia 不能为负")
        if self.persona_recent_limit < 1:
            raise ValueError("config.persona_recent_limit 至少为 1")
        if self.persona_history_limit < 1:
            raise ValueError("config.persona_history_limit 至少为 1")
        if not 0 <= self.persona_drift_warn < self.persona_drift_rupture <= 1:
            raise ValueError("漂移阈值必须满足 0 ≤ warn < rupture ≤ 1")
        if not 0 <= self.persona_peak_weight <= 1:
            raise ValueError("config.persona_peak_weight 必须落在 [0, 1]")
        if self.persona_churn_min_switches < 2:
            raise ValueError("config.persona_churn_min_switches 至少为 2")
        if self.persona_velocity_window_days <= 0:
            raise ValueError("config.persona_velocity_window_days 必须为正")
        if self.persona_velocity_warn <= 0:
            raise ValueError("config.persona_velocity_warn 必须为正")
        if not 0 <= self.persona_consistency_tolerance <= 1:
            raise ValueError("config.persona_consistency_tolerance 必须落在 [0, 1]")
        if self.persona_churn_per_hour <= 0:
            raise ValueError("config.persona_churn_per_hour 必须为正")
        # —— 自修正 ——
        if not 0 < self.selfcorrect_max_step_ratio < 1:
            raise ValueError("config.selfcorrect_max_step_ratio 必须落在 (0, 1)")
        if self.selfcorrect_verify_hours <= 0:
            raise ValueError("config.selfcorrect_verify_hours 必须为正")
        if self.selfcorrect_max_per_cycle < 0:
            raise ValueError("config.selfcorrect_max_per_cycle 不能为负")
        if self.selfcorrect_max_reverts < 1:
            raise ValueError("config.selfcorrect_max_reverts 至少为 1")
        if not 0 <= self.selfcorrect_effect_min < 1:
            raise ValueError("config.selfcorrect_effect_min 必须落在 [0, 1)")
        if self.selfcorrect_max_extensions < 0:
            raise ValueError("config.selfcorrect_max_extensions 不能为负")
        if not 0 < self.selfcorrect_settle_ratio <= 1:
            raise ValueError("config.selfcorrect_settle_ratio 必须落在 (0, 1]")

    @classmethod
    def from_dict(cls, d: dict[str, Any], strict: bool = True) -> "PalaceConfig":
        fields = dataclass_field_names(cls)
        unknown, missing = set(d) - fields, fields - set(d)
        if strict and unknown:
            raise ValueError(f"快照含未知配置项（版本不匹配？）: {sorted(unknown)}")
        if strict and missing:
            raise ValueError(f"快照缺少配置项: {sorted(missing)}")
        cfg = cls(**{k: v for k, v in d.items() if k in fields})
        cfg.validate()
        return cfg


@dataclass
class MemoryNode:
    name: str
    content: str = ""
    tier: str = "ephemeral"
    tags: tuple[str, ...] = ()
    created_at: float = 0.0
    last_accessed: float = 0.0
    base_weight: float = 1.0
    access_count: int = 1
    pinned: bool = False
    origin: str = "external"  # external / consolidated / reflected
    consolidated_from: tuple[str, ...] = ()
    consolidated_into: str | None = None
    fingerprint: str | None = None
    revision: int = 0
    affect: Affect = field(default_factory=Affect)
    narrative_hits: int = 0  # 被讲述进自述的累计次数（审计用，不直接当地位）
    # 叙事地位是**会退潮的**：heat 是最近几次讲述的泄漏积分，narrated_at 是最后一次被讲述的时刻。
    # 只有累计次数没有时间，等于给每条记忆发一张终身爵位 —— 讲过一次就永远「重要」。
    narrative_heat: float = 0.0
    narrated_at: float = 0.0
    kind: str = ""  # 记忆类型（空 = 未分类，由 MemoryModel 推断并写回）

    MAX_W: ClassVar[float] = 10.0
    AFFECT_GAIN: ClassVar[float] = 1.8

    def narrative_status(
        self, now: float, cap: int, half_life_days: float
    ) -> dict[str, float]:
        """叙事地位：累计次数按「上次被讲述」的时间退潮，再与近期热度取大。

        三个量分开，是为了让「被讲过」和「正在被讲」不再是同一件事：
          · count_ratio  累计讲述的份额（会随时隔衰减）
          · heat         近期讲述的泄漏积分（讲一次涨一格，不讲就掉）
          · central      两者取大 —— 价值判断只读这一个数
        """
        cap = max(1, int(cap))
        half_life = max(1e-6, float(half_life_days))
        reference = self.narrated_at if self.narrated_at > 0 else self.created_at
        days = max(0.0, (now - reference) / DAY)
        decay = 0.5 ** (days / half_life)
        effective_hits = self.narrative_hits * decay
        count_ratio = min(1.0, effective_hits / cap)
        heat = _clamp01(self.narrative_heat * decay)
        return {
            "hits": float(self.narrative_hits),
            "effective_hits": round(effective_hits, 6),
            "count_ratio": round(count_ratio, 6),
            "heat": round(heat, 6),
            "central": round(min(1.0, max(count_ratio, heat)), 6),
        }

    def touch_narrative(self, now: float, cap: int) -> None:
        """被讲进故事：次数 +1，热度涨一格，并重新开始计时。

        热度用 `(1 - heat) / cap` 的增量，所以讲 cap 次才逼近满格 ——
        与「次数 / cap」口径同量级，但**不讲就会掉**。
        """
        cap = max(1, int(cap))
        current = _clamp01(self.narrative_heat)
        self.narrative_heat = _clamp01(current + (1.0 - current) / cap)
        self.narrated_at = now
        self.narrative_hits += 1

    def stability(self, now: float | None = None, affect_gain: float | None = None) -> float:
        """记忆稳定性（天）：层级 + 访问次数（对数饱和）+ 访问时间跨度 + 情绪加固。

        情绪这一项是「怎么抓都抓不住的东西，偏偏最难忘」的机制解释：
        它乘在稳定性上，不是加成在权重上 —— 所以它不改变此刻的亮度，只改变衰减的速度。
        """
        span_days = max(0.0, (self.last_accessed - self.created_at) / DAY)
        tier_factor = TIER_STABILITY.get(self.tier, 1.0)
        # 记忆类型决定寿命量级：工作记忆以小时计，身份记忆几乎不衰减。
        from .memory_model import KIND_STABILITY  # 延迟导入：避免 core ↔ memory_model 循环

        kind_factor = KIND_STABILITY.get(self.kind, 1.0)
        gain = self.AFFECT_GAIN if affect_gain is None else affect_gain
        reference = self.last_accessed if now is None else now
        hardening = 1.0 + self.affect.stability_bonus(reference, gain)
        return tier_factor * kind_factor * (
            1.0
            + 0.75 * math.log1p(self.access_count)
            + 0.25 * math.sqrt(span_days)
        ) * hardening

    def effective_weight(self, now: float, affect_gain: float | None = None) -> float:
        """纯函数：任意时刻的有效强度。调用 N 次结果完全一致。"""
        if self.pinned:
            return self.base_weight
        days = max(0.0, (now - self.last_accessed) / DAY)
        return self.base_weight * math.exp(-days / self.stability(now, affect_gain))

    def access(self, now: float, gain: float = 0.5) -> None:
        """先结算已发生的衰减，再抬高锚点，避免把遗忘吞掉。"""
        w = self.effective_weight(now)
        self.base_weight = min(self.MAX_W, w + gain)
        self.access_count += 1
        self.last_accessed = now

    def should_forget(self, now: float, forget_w: float) -> bool:
        """物理层的快速判断：强度是否已经掉到底线。

        注意：这不是系统的遗忘判据。真正的取舍在 value.ValueModel ——
        「强度够了」不等于「值得留」，「强度不够」也不等于「该放手」（情绪与叙事会托住它）。
        这里保留它只为兼容旧调用与单节点测试。
        """
        return not self.pinned and self.effective_weight(now) <= forget_w

    def validate(self) -> None:
        """损坏的节点不能静默进入系统：NaN 权重会让衰减悄悄失效。"""
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("MemoryNode.name 必须是非空字符串")
        for key in ("created_at", "last_accessed", "base_weight"):
            value = getattr(self, key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{self.name}.{key} 类型非法: {value!r}")
            if not math.isfinite(float(value)):
                raise ValueError(f"{self.name}.{key} 不是有限数: {value!r}")
        if (
            isinstance(self.access_count, bool)
            or not isinstance(self.access_count, int)
            or self.access_count < 1
        ):
            raise ValueError(f"{self.name}.access_count 非法: {self.access_count!r}")
        if not isinstance(self.tags, (tuple, list)):
            raise ValueError(f"{self.name}.tags 类型非法")
        if self.kind:
            from .memory_model import KINDS  # 拼错的类型名必须当场炸，不能默默当情景记忆

            if self.kind not in KINDS:
                raise ValueError(f"{self.name}.kind 未知: {self.kind!r}")
        self.affect.validate(f"{self.name}.affect")
        if (
            isinstance(self.narrative_hits, bool)
            or not isinstance(self.narrative_hits, int)
            or self.narrative_hits < 0
        ):
            raise ValueError(f"{self.name}.narrative_hits 非法: {self.narrative_hits!r}")
        if (
            isinstance(self.narrative_heat, bool)
            or not isinstance(self.narrative_heat, (int, float))
            or not math.isfinite(float(self.narrative_heat))
            or not 0.0 <= float(self.narrative_heat) <= 1.0
        ):
            raise ValueError(f"{self.name}.narrative_heat 非法: {self.narrative_heat!r}")
        if (
            isinstance(self.narrated_at, bool)
            or not isinstance(self.narrated_at, (int, float))
            or not math.isfinite(float(self.narrated_at))
            or float(self.narrated_at) < 0
        ):
            raise ValueError(f"{self.name}.narrated_at 非法: {self.narrated_at!r}")

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["tags"] = list(self.tags)
        d["consolidated_from"] = list(self.consolidated_from)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "MemoryNode":
        d = dict(d)
        d["tags"] = tuple(d.get("tags", ()))
        d["consolidated_from"] = tuple(d.get("consolidated_from", ()))
        d["affect"] = Affect.from_dict(d.get("affect"))
        known = {k: v for k, v in d.items() if k in dataclass_field_names(cls)}
        node = cls(**known)
        node.validate()
        return node


@dataclass
class Edge:
    """共现边：记录两条记忆被一起检索的次数，是沉淀的结构依据。"""

    weight: float = 1.0
    coactivations: int = 1
    last_coactivated: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Edge":
        return cls(
            **{k: v for k, v in d.items() if k in dataclass_field_names(cls)}
        )


@dataclass
class Event:
    """系统自己的历史：一切反思都从这里取材。"""

    ts: float
    kind: str  # recall / miss / gc / consolidate / crystallize / decrystallize / reflect / restore
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"ts": self.ts, "kind": self.kind, "payload": self.payload}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Event":
        return cls(ts=float(d["ts"]), kind=str(d["kind"]), payload=dict(d.get("payload", {})))


class MemoryGraph:
    """零依赖的轻量无向图；nodes(data=True) 兼容 networkx 习惯。"""

    def __init__(self) -> None:
        self._nodes: dict[str, MemoryNode] = {}
        self._adj: dict[str, dict[str, Edge]] = {}

    def __len__(self) -> int:
        return len(self._nodes)

    def __contains__(self, name: object) -> bool:
        return name in self._nodes

    def nodes(self, data: bool = False):
        if data:
            return [(name, {"object": node}) for name, node in self._nodes.items()]
        return list(self._nodes.keys())

    def all_nodes(self) -> list[MemoryNode]:
        return list(self._nodes.values())

    def get(self, name: str) -> MemoryNode | None:
        return self._nodes.get(name)

    def add_node(self, node: MemoryNode) -> MemoryNode:
        if node.name in self._nodes:
            return self._nodes[node.name]
        self._nodes[node.name] = node
        self._adj.setdefault(node.name, {})
        return node

    def remove_nodes_from(self, names: Iterable[str]) -> list[str]:
        removed = []
        for name in names:
            if name not in self._nodes:
                continue
            self._nodes.pop(name)
            for nb in list(self._adj.get(name, {})):
                self._adj.get(nb, {}).pop(name, None)
            self._adj.pop(name, None)
            removed.append(name)
        return removed

    def link(self, a: str, b: str, now: float, gain: float = 1.0) -> Edge | None:
        if a == b or a not in self._nodes or b not in self._nodes:
            return None
        self._adj.setdefault(a, {})
        self._adj.setdefault(b, {})
        e = self._adj[a].get(b)
        if e is None:
            e = Edge(weight=gain, coactivations=1, last_coactivated=now)
            self._adj[a][b] = e
            self._adj[b][a] = e
        else:
            e.weight += gain
            e.coactivations += 1
            e.last_coactivated = now
        return e

    def edge(self, a: str, b: str) -> Edge | None:
        return self._adj.get(a, {}).get(b)

    def neighbors(self, name: str) -> dict[str, Edge]:
        return dict(self._adj.get(name, {}))

    def coactivate(self, names: Iterable[str], now: float) -> None:
        uniq = [n for n in dict.fromkeys(names) if n in self._nodes]
        for i in range(len(uniq)):
            for j in range(i + 1, len(uniq)):
                self.link(uniq[i], uniq[j], now)

    def edges(self) -> list[tuple[str, str, Edge]]:
        seen: set[tuple[str, str]] = set()
        out: list[tuple[str, str, Edge]] = []
        for a, nbrs in self._adj.items():
            for b, e in nbrs.items():
                key = (a, b) if a < b else (b, a)
                if key in seen:
                    continue
                seen.add(key)
                out.append((key[0], key[1], e))
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes": [n.to_dict() for n in self._nodes.values()],
            "edges": [[a, b, e.to_dict()] for a, b, e in self.edges()],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "MemoryGraph":
        g = cls()
        for nd in d.get("nodes", []):
            node = MemoryNode.from_dict(nd)
            if node.name in g:
                raise ValueError(f"快照含重复节点: {node.name}")
            g.add_node(node)
        for a, b, ed in d.get("edges", []):
            if a not in g or b not in g:
                raise ValueError(f"快照含悬空边: {a} ↔ {b}")
            e = Edge.from_dict(ed)
            g._adj.setdefault(a, {})[b] = e
            g._adj.setdefault(b, {})[a] = e
        return g


TEMP_PREFIX = ".palace-"
TEMP_SUFFIX = ".tmp"
# 陈旧临时文件只回收「足够老」的：并发写时别人的 tmp 可能才刚落地。
ORPHAN_MIN_AGE = 300.0
# 写盘的可观测账本：不落盘的成功，也要能说清「目录项到底持久化没有」。
WRITE_STATS: dict[str, int] = {
    "writes": 0,
    "dir_fsync_ok": 0,
    "dir_fsync_degraded": 0,
    "orphans": 0,
    "cleanup_retries": 0,
    "swept": 0,
}


def _flush_dir_windows(path: str) -> bool:
    """Windows 上对目录 fsync 的可行替代：打开目录句柄 + FlushFileBuffers。

    `os.open(dir, O_RDONLY)` 在 Windows 上直接抛 PermissionError，所以旧实现
    永远是「静默降级」—— 文件内容落盘了，但目录项（改名）没有持久化保证。
    这里用 FILE_FLAG_BACKUP_SEMANTICS 拿到目录句柄，真正刷一次。
    """
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        create_file.restype = wintypes.HANDLE
        flush = kernel32.FlushFileBuffers
        flush.argtypes = [wintypes.HANDLE]
        flush.restype = wintypes.BOOL
        close = kernel32.CloseHandle
        close.argtypes = [wintypes.HANDLE]
        close.restype = wintypes.BOOL

        generic_read, generic_write = 0x80000000, 0x40000000
        share_all = 0x00000001 | 0x00000002 | 0x00000004
        open_existing, backup_semantics = 3, 0x02000000
        invalid = ctypes.c_void_p(-1).value

        handle = create_file(
            str(path),
            generic_read | generic_write,
            share_all,
            None,
            open_existing,
            backup_semantics,
            None,
        )
        if handle is None or handle == invalid:
            return False
        try:
            return bool(flush(handle))
        finally:
            close(handle)
    except Exception:  # noqa: BLE001 - 拿不到句柄只意味着降级，不是业务失败
        return False


def _fsync_dir(path: str) -> bool:
    """返回「目录项是否真的持久化」。失败不再静默，而是记进 WRITE_STATS。"""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return _flush_dir_windows(path)
    try:
        os.fsync(fd)
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def _sweep_stale_temps(parent: str, min_age: float = ORPHAN_MIN_AGE) -> int:
    """回收上一次「replace + unlink 双双失败」留下的孤儿临时文件。

    双失败无法在当次调用里挽救，但泄漏必须有界：下一次写盘顺手把够老的清掉。
    只认自己的前缀/后缀，且只清够老的 —— 不碰别人正在写的文件。
    """
    removed = 0
    try:
        names = os.listdir(parent)
    except OSError:
        return 0
    now = time.time()
    for name in names:
        if not (name.startswith(TEMP_PREFIX) and name.endswith(TEMP_SUFFIX)):
            continue
        full = os.path.join(parent, name)
        try:
            if now - os.path.getmtime(full) < min_age:
                continue
            os.unlink(full)
            removed += 1
        except OSError:
            continue
    if removed:
        WRITE_STATS["swept"] += removed
    return removed


def _cleanup_temp(tmp: str, attempts: int = 3) -> bool:
    """删临时文件；失败重试（Windows 上的占用/共享冲突常常是一次性的）。"""
    for attempt in range(attempts):
        if not os.path.exists(tmp):
            return True
        try:
            os.unlink(tmp)
            return True
        except OSError as exc:
            if attempt == attempts - 1:
                WRITE_STATS["orphans"] += 1
                warnings.warn(
                    f"临时文件清理失败（已重试 {attempts} 次，将在下次写盘时回收）: {tmp}: {exc}",
                    RuntimeWarning,
                )
                return False
            WRITE_STATS["cleanup_retries"] += 1
            time.sleep(0.01 * (attempt + 1))
    return False


def atomic_write_json(path: str, obj: Any) -> bool:
    """临时文件 + 文件 fsync + 原子 replace + 目录 fsync（含 Windows 路径）。

    返回「目录项是否持久化」：False 表示内容已写入但断电后可能回滚到旧快照，
    调用方可据此告警。任何一步失败都不得污染旧快照；临时文件由重试清理兜底，
    彻底删不掉时记账并留待下次写盘回收 —— 泄漏有界，而不是每次多一个。
    """
    parent = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(parent, exist_ok=True)
    _sweep_stale_temps(parent)
    fd, tmp = tempfile.mkstemp(prefix=TEMP_PREFIX, suffix=TEMP_SUFFIX, dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        durable = _fsync_dir(parent)
    finally:
        _cleanup_temp(tmp)
    WRITE_STATS["writes"] += 1
    WRITE_STATS["dir_fsync_ok" if durable else "dir_fsync_degraded"] += 1
    return durable


def read_json(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


class MutableClock:
    """可推进的注入时钟：让「时间」在测试里变成可操纵的变量。"""

    def __init__(self, t: float | None = None) -> None:
        self.t = time.time() if t is None else t
        self._lock = threading.Lock()

    def __call__(self) -> float:
        return self.t

    def advance(self, days: float) -> None:
        with self._lock:
            self.t += days * DAY
