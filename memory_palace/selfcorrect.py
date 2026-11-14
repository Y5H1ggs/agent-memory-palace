"""selfcorrect.py - 自修正：能发现自己错了，并且改得起、退得回。

自修正不是「自己改自己」。区别就在四条边界上，缺一条都会变成自毁：

  范围  只能动**派生参数**（白名单）。基座、人设、钉死的记忆一律不在其内。
        改基座不是修正，是换人；改人设不是修正，是换面具。这两件事只能人来做。
        白名单不是越窄越安全：只有 7 个旋钮时，绝大多数症状都无处可修，等于没修。
        本版扩到「表现层可调」的一整组，并给每个参数配**值域**（PARAM_BOUNDS）。
  幅度  单次不超过当前值的 max_step_ratio，且不越出该参数自己的值域。
  冷却  同一参数在冷却期内不许再调。没有冷却，两个相邻指标会把它推成钟摆。
  可逆  每次修正是一个 Patch，带 before / after / 验证指标 / 观察窗。
        窗口结束若指标没有**变好**，自动回滚 —— 没有证据的改动不构成保留理由。

再加两条兜底：

  · 同一参数被回滚 max_reverts 次以上，判定为「不可自动调参」，永久交还给人。
  · **样本比观察窗还旧，就不许判**。旧实现拿「本轮反思算出来的指标」去判一个
    上一轮才生效的修正，而该指标的统计窗口还横跨着修正之前的时间 —— 这是用
    滞后一期的数据判当期决策。现在：
      - 每个 pending 修正只在**观察窗之后**取样本（verify_at 之前的样本只当基线）；
      - 判据要跑赢噪声（变化幅度 ≥ effect_min），否则视为无效；
      - 样本太旧就延期，延期到上限仍拿不到新鲜样本 → 判「无证据」，回滚。
        没有办法证明有效，就不保留 —— 这与「没法证明有罪就无罪」正好相反，
        因为保留一个未验证的改动，代价是系统长期跑在错误参数上。
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any

from .core import Event, PalaceConfig

HOUR = 3600.0


def _median(values: list[float]) -> float:
    """中位数。自己写而不是 import statistics —— 标准库那个模块光导入就要 17ms，
    而 `memory-palace` 是每次调用都新起一个进程的 CLI，这 17ms 每次都要付。
    这里只用到一个函数，五行就够。"""
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0

# 白名单：这些是「表现层旋钮」，调错了也只是行为偏差，不会伤到身份与基座。
ALLOWED_PARAMS: frozenset[str] = frozenset(
    {
        "importance_forget",
        "semantic_min_score",
        "semantic_relative_floor",
        "style_min_span_days",
        "consolidate_similarity",
        "consolidate_min_shared_tags",
        "narrative_max_chapters",
        "narrative_select_importance",
        "narrative_relative_floor",
        "grasp_budget",
        "logic_spread_threshold",
        "affect_distortion",
    }
)

# 每个可调参数自己的值域：全局 [1e-4, 1] 太粗，「章节数上限」和「百分比门槛」不是一回事。
PARAM_BOUNDS: dict[str, tuple[float, float]] = {
    "importance_forget": (0.005, 0.5),
    "semantic_min_score": (0.02, 0.6),
    "semantic_relative_floor": (0.20, 0.95),
    "style_min_span_days": (0.0, 30.0),
    "consolidate_similarity": (0.20, 0.95),
    "consolidate_min_shared_tags": (1.0, 4.0),
    "narrative_max_chapters": (1.0, 12.0),
    "narrative_select_importance": (0.02, 0.6),
    "narrative_relative_floor": (0.05, 0.9),
    "grasp_budget": (0.0, 20.0),
    "logic_spread_threshold": (0.02, 0.9),
    "affect_distortion": (0.0, 0.9),
}

# 明令禁止：写在这里不是为了执行，是为了**能被测试读到**。
# 这些参数一动，改的就不是「表现」而是「身份 / 记忆的物理常数」。
# 护栏指标：主指标改善不够，还要看这些没有被牺牲。
# 单指标优化一定会挤占别的指标（把召回门槛压低，噪声就上来了），
# 而账面上只看到好的一面 —— 这就是「自修正的变好仍是单指标」这条批评的根。
# 方向写清楚很重要：`forget_pressure` 是「越低越好」，`retrieval_coverage` 是「越高越好」，
# 弄反了会得到一个「越改越差才留下」的系统。
GUARD_DIRECTIONS: dict[str, str] = {
    "retrieval_coverage": "higher",
    "consolidations": "higher",
    "recalls": "higher",
    "loose_fragments": "lower",
    "forget_pressure": "lower",
    "narrative_monopoly": "lower",
    "narrative_reliability": "higher",
    "persona_drift": "lower",
    "persona_oversell": "lower",
    "structure_isolated": "lower",
    "gc_deletes": "lower",
}

FORBIDDEN_PARAMS: frozenset[str] = frozenset(
    {
        "persona_learning_rate",
        "persona_drift_warn",
        "persona_consistency_tolerance",
        "forget_w",
        "max_events",
        "affect_stability_gain",
        "logic_hop_decay",
    }
)


@dataclass
class Patch:
    """一次修正的完整凭据。没有这个对象的改动，不算自修正，只算乱动。"""

    pid: str
    param: str
    before: float
    after: float
    code: str
    reason: str
    created_at: float
    verify_at: float
    metric: str
    baseline: float
    better: str  # "lower" | "higher"
    status: str = "pending"  # pending / kept / reverted / rejected
    observed: float | None = None
    applied_at: float = 0.0
    observed_at: float = 0.0
    samples: list[list[float]] = field(default_factory=list)
    # 护栏基线与判决时的越界情况：只记「当时是多少」，不记「应该多少」——
    # 阈值属于配置，不属于凭据。
    guards: dict[str, float] = field(default_factory=dict)
    guard_hits: dict[str, float] = field(default_factory=dict)
    deferred: int = 0
    evidence: str = "pending"  # pending / window_samples / baseline_only / stale

    def improved(self, value: float) -> bool:
        if self.better == "lower":
            return value < self.baseline - 1e-9
        return value > self.baseline + 1e-9

    def effect(self, value: float) -> float:
        return abs(value - self.baseline) / max(abs(self.baseline), 1e-9)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Patch":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class Candidate:
    """检测到的可修正信号。只是候选，还没动手。"""

    code: str
    param: str
    direction: str  # "-" 下调 / "+" 上调
    reason: str
    metric: str
    better: str


@dataclass
class Escalation:
    """检测到但**不该由系统自己修**的问题：只能上报给人。"""

    code: str
    reason: str
    suggested_owner: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SelfCorrector:
    def __init__(self, config: PalaceConfig) -> None:
        self.config = config
        self.patches: list[Patch] = []
        self.cooldowns: dict[str, float] = {}
        self.reverted_count: dict[str, int] = {}
        self.blocked: set[str] = set()
        self.escalations: list[Escalation] = []
        self._seq = 0

    # ---- 检测 ----

    def detect(self, metrics: dict[str, Any], now: float) -> tuple[list[Candidate], list[Escalation]]:
        """从指标里读出系统自己的症状。每一条都指向一个具体旋钮，或指向「别自己动」。"""
        cfg = self.config
        cands: list[Candidate] = []
        esc: list[Escalation] = []
        seen: set[str] = set()

        def add(code: str, param: str, direction: str, reason: str, metric: str, better: str) -> None:
            if param in seen or param in self.blocked:
                return
            seen.add(param)
            cands.append(Candidate(code, param, direction, reason, metric, better))

        restores = metrics.get("restores", 0)
        if restores and restores > 0:
            add(
                "resurrection",
                "importance_forget",
                "-",
                f"窗口内 {int(restores)} 次误删恢复：取舍门槛偏激进，把还该留的当成了负担。",
                "restores",
                "lower",
            )

        pressure = metrics.get("forget_pressure", 0.0)
        if pressure > 0.30:
            add(
                "mass_forgetting",
                "importance_forget",
                "-",
                f"单轮清理占比 {pressure:.0%}：遗忘太快，记忆来不及被沉淀就被扫走。",
                "forget_pressure",
                "lower",
            )

        decry = metrics.get("decrystallizations", 0)
        if decry and decry > 0:
            add(
                "style_flip",
                "style_min_span_days",
                "+",
                f"{int(decry)} 条风格固化后被反证撤销：观察期太短，把一时兴起当成了稳定偏好。",
                "decrystallizations",
                "lower",
            )

        coverage = metrics.get("retrieval_coverage", 1.0)
        # 样本太少时覆盖率没有意义：3 次检索里 1 次命中，不该被判成「召回路径过窄」。
        if coverage < 0.5 and metrics.get("retrieval_attempts", 0) >= 5:
            add(
                "retrieval_narrow",
                "semantic_min_score",
                "-",
                f"检索覆盖率 {coverage:.0%}：召回门槛过高，很多东西想不起来。",
                "retrieval_coverage",
                "higher",
            )

        loose = metrics.get("loose_fragments", 0)
        if loose >= cfg.consolidate_min_group and metrics.get("consolidations", 0) == 0:
            add(
                "no_crystallization",
                "consolidate_similarity",
                "-",
                f"{int(loose)} 条碎片零沉淀：聚类门槛过严，碎片永远长不成结论。",
                "consolidation_yield",
                "higher",
            )

        monopoly = metrics.get("narrative_monopoly", 0.0)
        if monopoly > cfg.narrative_monopoly_warn:
            add(
                "story_narrowing",
                "narrative_max_chapters",
                "+",
                f"自述单主题占比 {monopoly:.0%}：故事越讲越窄，需要更多章节容量容纳别的线索。",
                "narrative_monopoly",
                "lower",
            )

        # —— 结构：价值有梯度但图是散的，这不是健康，是「什么都没连上」——
        # 只在「这一窗口一次沉淀都没有」时才归咎于聚类门槛 ——
        # 否则「早期还没连上」会被误判成「门槛过高」，把好参数往下推。
        isolated = metrics.get("structure_isolated")
        if (
            isolated is not None
            and isolated >= cfg.structure_isolated_warn
            and metrics.get("consolidations", 0) == 0
        ):
            add(
                "structural_isolation",
                "consolidate_similarity",
                "-",
                f"{isolated:.0%} 的记忆一根边都没有：聚类门槛可能过高，碎片从未互相连上。",
                "structure_isolated",
                "lower",
            )

        # —— 检索：语义检索在跑，却一次都没召回标签不重叠的记忆 ——
        sem_recalls = metrics.get("semantic_recalls", 0.0)
        sem_only = metrics.get("semantic_only_hits")
        if sem_only is not None and sem_recalls >= 5 and sem_only == 0:
            add(
                "semantic_degenerate",
                "semantic_relative_floor",
                "-",
                f"{int(sem_recalls)} 次语义检索零「语义独有命中」：相对门槛把非字面命中全挡掉了。",
                "semantic_only_hits",
                "higher",
            )

        # —— 注意力：预算全花光却仍有一大半记忆没被抓住 ——
        ungrabbed = metrics.get("ungrabbed_share")
        if ungrabbed is not None and ungrabbed > 0.85:
            add(
                "grasp_starvation",
                "grasp_budget",
                "+",
                f"本轮 {ungrabbed:.0%} 的记忆没被抓住：代谢预算相对于存量已经太薄。",
                "ungrabbed_share",
                "lower",
            )

        # —— 这里必须「不修」：超出权限的，一律上报 ——
        oversell = metrics.get("persona_oversell", 0.0)
        if oversell > cfg.persona_consistency_tolerance:
            esc.append(
                Escalation(
                    "persona_overreach",
                    f"人设要求的表达超出人格支撑 {oversell:.2f}。"
                    "这该由人决定：把要求降下来（诚实），还是继续喂证据（成长）—— 系统无权替你选。",
                    "human",
                )
            )
        drift = metrics.get("persona_drift", 0.0)
        if drift > cfg.persona_drift_rupture:
            esc.append(
                Escalation(
                    "persona_drift",
                    f"人格距基座 {drift:.2f}，已越过断裂阈值。修宪还是纠偏，只有人能定。",
                    "human",
                )
            )
        return cands, esc

    # ---- 提议 ----

    def _step(self, current: float, direction: str, bounds: tuple[float, float]) -> float:
        """一次只走一步，且不越过这个参数自己的值域。修正不是重写。"""
        ratio = self.config.selfcorrect_max_step_ratio
        delta = max(abs(current) * ratio, 1e-4)
        target = current - delta if direction == "-" else current + delta
        low, high = bounds
        return round(max(low, min(high, target)), 6)

    def propose(
        self, candidates: list[Candidate], metrics: dict[str, Any], now: float
    ) -> list[Patch]:
        out: list[Patch] = []
        for cand in candidates[: self.config.selfcorrect_max_per_cycle]:
            if cand.param not in ALLOWED_PARAMS:
                continue  # 白名单是硬约束，不是建议（读模块级全局，便于被测试替换）
            if cand.param in self.blocked:
                continue  # 反复改错的参数已交还给人；detect 会拦，这里再拦一次
            if now < self.cooldowns.get(cand.param, 0.0):
                continue  # 冷却中：上一个修正还没验完，不许叠
            current = getattr(self.config, cand.param)
            bounds = PARAM_BOUNDS.get(cand.param, (1e-4, 1.0))
            if cand.param in PalaceConfig.INT_FIELDS:
                low = max(1, int(math.ceil(bounds[0])))
                high = max(low, int(bounds[1]))
                after = int(current - 1 if cand.direction == "-" else current + 1)
                after = max(low, min(high, after))
            else:
                after = self._step(float(current), cand.direction, bounds)
            if abs(after - current) < 1e-9:
                continue
            self._seq += 1
            out.append(
                Patch(
                    pid=f"p{self._seq}",
                    param=cand.param,
                    before=float(current),
                    after=float(after),
                    code=cand.code,
                    reason=cand.reason,
                    created_at=now,
                    verify_at=now + self.config.selfcorrect_verify_hours * HOUR,
                    metric=cand.metric,
                    baseline=float(metrics.get(cand.metric, 0.0) or 0.0),
                    better=cand.better,
                    guards={
                        key: float(metrics[key] or 0.0)
                        for key in GUARD_DIRECTIONS
                        if key in metrics and metrics[key] is not None
                    },
                )
            )
        return out

    # ---- 执行 ----

    def apply(self, patch: Patch, now: float | None = None) -> bool:
        """真的写进配置。写完立刻全量校验，坏了就当场退回。"""
        old = getattr(self.config, patch.param, None)
        setattr(self.config, patch.param, patch.after)
        try:
            self.config.validate()
        except ValueError:
            setattr(self.config, patch.param, old)
            patch.status = "rejected"
            return False
        patch.status = "pending"
        patch.applied_at = patch.created_at if now is None else now
        self.cooldowns[patch.param] = patch.verify_at
        self.patches.append(patch)
        return True

    # ---- 观察：先把样本攒起来，再谈判 ----

    def observe(self, metrics: dict[str, Any], now: float) -> None:
        """每个周期把「当前指标」记一笔，且只记 pending 修正关心的指标。

        这条账本是抗滞后的关键：判据可以只看 verify_at 之后的样本，
        而不是拿一个横跨修正前后的窗口平均值去冒充「修正后的表现」。
        """
        for patch in self.patches:
            if patch.status != "pending":
                continue
            if patch.metric not in metrics:
                continue
            patch.samples.append([float(now), float(metrics[patch.metric] or 0.0)])
            if len(patch.samples) > 60:
                patch.samples = patch.samples[-60:]

    # ---- 验证 ----

    def rollback_unpersisted(self, patch: Patch, reason: str = "save_failed") -> bool:
        """改在内存里、但没能落盘时，把配置退回去。

        这不是「验证失败」，是「这次修改从未真正生效过」：
        磁盘上还是旧值，重启就会回滚，而当前进程却拿着新值继续跑 ——
        内存与磁盘从此说的不是同一件事，且**没有人会收到报错**。

        原则：**存不下，就不算改过。** 提交是修改的一部分，不是修改之后的额外步骤。
        """
        if patch.status not in ("pending", "kept"):
            return False
        setattr(self.config, patch.param, patch.before)
        patch.status = "reverted"
        patch.reason = f"{patch.reason}｜{reason}"
        return True

    def _guard_hits(
        self, patch: Patch, metrics: dict[str, Any] | None
    ) -> dict[str, float]:
        """护栏越界检测：主指标可以变好，但不许拿别的指标去换。

        `margin` 是相对变化：0.15 表示「掉了 15% 以上才算牺牲」。
        设 0 会让噪声频繁触发回滚；设太大等于没有护栏。
        """
        if not metrics or not patch.guards:
            return {}
        margin = self.config.selfcorrect_guard_margin
        hits: dict[str, float] = {}
        for key, base in patch.guards.items():
            direction = GUARD_DIRECTIONS.get(key)
            if direction is None or key not in metrics or metrics[key] is None:
                continue
            try:
                current = float(metrics[key])
            except (TypeError, ValueError):
                continue
            if abs(base) < 1e-9:
                continue
            delta = (current - base) / abs(base)
            worse = delta < -margin if direction == "higher" else delta > margin
            if worse:
                hits[key] = round(delta, 4)
        return hits

    def _judge(
        self,
        patch: Patch,
        value: float,
        evidence: str,
        now: float,
        metrics: dict[str, Any] | None = None,
    ) -> str:
        """统一判决：主指标要变好、幅度跑赢噪声，**且**没有牺牲护栏指标，才留。

        三个 v0.9 时代就有的纪律不变（样本新鲜度、窗口后样本、幅度门槛），
        这里新增的是第四道：多目标。
        """
        patch.observed = value
        patch.observed_at = now
        patch.evidence = evidence
        improved = patch.improved(value) and patch.effect(value) >= (
            self.config.selfcorrect_effect_min
        )
        if improved:
            hits = self._guard_hits(patch, metrics)
            if not hits:
                patch.status = "kept"
                return "kept"
            # 主指标好了，但代价是别的指标崩了 —— 不算「变好」
            patch.guard_hits = hits
            patch.evidence = f"{evidence}+guardrail"
            patch.reason = f"{patch.reason}｜护栏越界 {sorted(hits)}"
        patch.status = "reverted"
        setattr(self.config, patch.param, patch.before)
        self.reverted_count[patch.param] = self.reverted_count.get(patch.param, 0) + 1
        if self.reverted_count[patch.param] >= self.config.selfcorrect_max_reverts:
            self.blocked.add(patch.param)  # 反复改错的参数，永久交还给人
        return "reverted"

    def verify(
        self, metrics: dict[str, Any], now: float, metrics_at: float | None = None
    ) -> list[Patch]:
        """观察窗到期：指标变好才留下，没变好就退回去，并记账。

        三条纪律（对应「验证指标滞后一轮」这个漏洞）：
          1. 样本时刻比观察窗还早 → 不判，延期（延期到上限仍不新鲜 → 判无证据并回滚）；
          2. 观察窗内的样本只当基线，判决只读窗口**之后**的样本中位数；
          3. 没有窗口后样本时退回单点比较，但要求幅度跑赢 effect_min，避免噪声冒充改善。
        """
        settled: list[Patch] = []
        at = now if metrics_at is None else metrics_at
        window = self.config.selfcorrect_verify_hours * HOUR
        for patch in self.patches:
            if patch.status != "pending" or now < patch.verify_at:
                continue
            if at + 1e-9 < patch.verify_at:
                # 指标快照比观察窗还旧 —— 用它判等于用上一轮的数据判这一轮的修正。
                patch.deferred += 1
                if patch.deferred <= self.config.selfcorrect_max_extensions:
                    patch.evidence = "deferred_stale_metric"
                    patch.verify_at = now + window * self.config.selfcorrect_settle_ratio
                    continue
                self._judge(
                    patch,
                    float(metrics.get(patch.metric, 0.0) or 0.0),
                    "stale",
                    now,
                    metrics,
                )
                settled.append(patch)
                continue

            after_window = [
                value for stamped, value in patch.samples if stamped >= patch.verify_at
            ]
            if after_window:
                value = _median(after_window)
                evidence = "window_samples"
            else:
                value = float(metrics.get(patch.metric, 0.0) or 0.0)
                evidence = "baseline_only"
            self._judge(patch, value, evidence, now, metrics)
            settled.append(patch)
        return settled

    def events(self, patches: list[Patch], now: float) -> list[Event]:
        return [
            Event(
                now,
                f"selfcorrect_{p.status}",
                {
                    "pid": p.pid,
                    "param": p.param,
                    "before": p.before,
                    "after": p.after,
                    "code": p.code,
                    "observed": p.observed,
                    "metric": p.metric,
                },
            )
            for p in patches
        ]

    # ---- 对外 ----

    def status(self) -> dict[str, Any]:
        return {
            "patches": [p.to_dict() for p in self.patches[-50:]],
            "pending": [p.pid for p in self.patches if p.status == "pending"],
            "kept": sum(1 for p in self.patches if p.status == "kept"),
            "reverted": sum(1 for p in self.patches if p.status == "reverted"),
            "blocked_params": sorted(self.blocked),
            "cooldowns": {k: round(v, 3) for k, v in self.cooldowns.items()},
            "escalations": [e.to_dict() for e in self.escalations],
            "allowed_params": sorted(ALLOWED_PARAMS),
            "bounds": {k: list(v) for k, v in sorted(PARAM_BOUNDS.items())},
            "forbidden_params": sorted(FORBIDDEN_PARAMS),
            "with_samples": sum(1 for p in self.patches if p.samples),
            # 多目标证据：哪些修正因为牺牲了别的指标而被退回
            "guardrail_reverts": [
                {"pid": p.pid, "param": p.param, "hits": p.guard_hits}
                for p in self.patches
                if p.guard_hits
            ],
            "guard_directions": dict(GUARD_DIRECTIONS),
            "guard_margin": self.config.selfcorrect_guard_margin,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "patches": [p.to_dict() for p in self.patches],
            "cooldowns": dict(self.cooldowns),
            "reverted_count": dict(self.reverted_count),
            "blocked": sorted(self.blocked),
            "escalations": [e.to_dict() for e in self.escalations],
            "seq": self._seq,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None, config: PalaceConfig) -> "SelfCorrector":
        obj = cls(config)
        d = d or {}
        obj.patches = [Patch.from_dict(p) for p in d.get("patches", [])]
        obj.cooldowns = {k: float(v) for k, v in (d.get("cooldowns") or {}).items()}
        obj.reverted_count = {k: int(v) for k, v in (d.get("reverted_count") or {}).items()}
        obj.blocked = set(d.get("blocked", []))
        obj.escalations = [
            Escalation(**e) for e in d.get("escalations", [])
        ]
        obj._seq = int(d.get("seq", len(obj.patches)))
        return obj
