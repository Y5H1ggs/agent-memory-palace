"""persona.py - 基座 → 人格 → 人设 → 变更：四层**不同的**可变性。

四者不是同一件事的四个阶段，而是四种性质完全不同的东西：

  基座 Foundation    宪法层。只能由人改写，系统只读。改动留痕、版本递增、
                     且拒绝与自身既有公理自相矛盾的修正。
  人格 Personality   从证据里长出来的连续量。有惯性：越是被反复观察的特质越难推动；
                     且永远不得越出基座给的硬包络（floor / ceiling）——
                     这就是「以基础人格逐步构筑」在代码里的确切含义。
  人设 Persona       面向场合的离散选择。同一时刻只有一套生效，可有多套备选。
                     它不是人格的复制品，而是**允许有夸张的投影**（tolerance 就是夸张额度）。
  变更 Revision      不是「重写」，而是「留痕的替换」：版本、理由、作者、父版本、可回滚。

为什么必须拆开：
  · 基座若能被系统自己改，它就不是基座，只是一条比较旧的记忆；
  · 人格若能瞬间翻转，它就不是人格，只是一个跟着输入走的变量；
  · 人设若不能切换，它就不是人设，只是行为习惯；
  · 变更若不留痕，就不是演化，只是覆盖。

两个关键判据（单靠阈值判不出来）：
  漂移 = 距离 × 速度。缓慢移动 0.3 是成长，一天内跳 0.3 是断裂。
  一致性 = 人设向外的表达 vs 人格实际的支撑。超额的只有「演得比实际强」
           （oversell），undersell 是克制，不算病。
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from .core import DAY, Event, PalaceConfig, dataclass_field_names

TRAIT_KEYS: tuple[str, ...] = (
    "rigor",
    "directness",
    "warmth",
    "curiosity",
    "caution",
    "playfulness",
    "persistence",
)


def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def _finite(x: Any, label: str) -> float:
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        raise ValueError(f"{label} 必须是数值，实际 {type(x).__name__}: {x!r}")
    value = float(x)
    if not math.isfinite(value):
        raise ValueError(f"{label} 不是有限数: {x!r}")
    return value


# --------------------------------------------------------------------------- #
# 基座
# --------------------------------------------------------------------------- #


@dataclass
class TraitSpec:
    """基座对某个维度的授权范围。floor/ceiling 是硬包络，人格不得越出。"""

    label: str
    baseline: float
    floor: float = 0.0
    ceiling: float = 1.0
    locked: bool = False  # 锁定 = 系统连包络之内的漂移都不许做

    def validate(self, key: str) -> None:
        label = f"foundation.traits[{key}]"
        for name in ("baseline", "floor", "ceiling"):
            value = _finite(getattr(self, name), f"{label}.{name}")
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{label}.{name} 必须落在 [0, 1]，实际 {value}")
        if self.floor > self.ceiling:
            raise ValueError(f"{label} 的下界 {self.floor} 高于上界 {self.ceiling}")
        if not self.floor <= self.baseline <= self.ceiling:
            raise ValueError(f"{label} 的 baseline 落在包络之外")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TraitSpec":
        known = {k: v for k, v in d.items() if k in dataclass_field_names(cls)}
        return cls(**known)


@dataclass
class Amendment:
    """修宪记录。人改基座也留痕 —— 否则「基础」会随时间变成传说。"""

    version: int
    at: float
    author: str
    reason: str
    changes: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Amendment":
        return cls(
            version=int(d["version"]),
            at=float(d["at"]),
            author=str(d["author"]),
            reason=str(d["reason"]),
            changes=list(d.get("changes", [])),
        )


@dataclass
class Foundation:
    """基础人格。系统只读 —— 唯一的写入口是 amend()，且必须带理由与作者。"""

    version: int = 1
    created_at: float = 0.0
    author: str = "user"
    axioms: tuple[str, ...] = ()
    traits: dict[str, TraitSpec] = field(default_factory=dict)
    amendments: list[Amendment] = field(default_factory=list)
    sealed: bool = False  # 一旦封印，任何 amend 都必须显式 force

    def validate(self) -> None:
        if self.version < 1:
            raise ValueError("foundation.version 至少为 1")
        if not isinstance(self.axioms, (tuple, list)):
            raise ValueError("foundation.axioms 必须是序列")
        if not self.traits:
            raise ValueError("基础人格不能为空：至少需要一个维度")
        for key, spec in self.traits.items():
            spec.validate(key)

    def labels(self) -> dict[str, str]:
        return {k: v.label for k, v in self.traits.items()}

    def envelope(self, key: str) -> tuple[float, float]:
        spec = self.traits[key]
        return spec.floor, spec.ceiling

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "created_at": self.created_at,
            "author": self.author,
            "axioms": list(self.axioms),
            "traits": {k: v.to_dict() for k, v in self.traits.items()},
            "amendments": [a.to_dict() for a in self.amendments],
            "sealed": self.sealed,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Foundation":
        if not isinstance(d, dict):
            raise ValueError("快照缺少 foundation 段")
        traits = {
            k: TraitSpec.from_dict(v) for k, v in (d.get("traits") or {}).items()
        }
        f = cls(
            version=int(d.get("version", 0)),
            created_at=float(d.get("created_at", 0.0)),
            author=str(d.get("author", "user")),
            axioms=tuple(d.get("axioms", ())),
            traits=traits,
            amendments=[Amendment.from_dict(a) for a in d.get("amendments", [])],
            sealed=bool(d.get("sealed", False)),
        )
        f.validate()
        return f


def default_foundation(now: float = 0.0, author: str = "user") -> Foundation:
    """默认基座：一套可直接跑的七维人格骨架，用户可整体替换。

    包络留得比较宽（0.15–0.95），因为「逐步构筑」需要空间；
    锁定的维度（rigor 下界）才是真正不许动的部分。
    """
    specs = {
        "rigor": TraitSpec("严谨", baseline=0.70, floor=0.45, ceiling=0.98),
        "directness": TraitSpec("直白", baseline=0.55, floor=0.10, ceiling=0.95),
        "warmth": TraitSpec("温度", baseline=0.55, floor=0.10, ceiling=0.95),
        "curiosity": TraitSpec("好奇", baseline=0.60, floor=0.15, ceiling=0.95),
        "caution": TraitSpec("审慎", baseline=0.60, floor=0.20, ceiling=0.95),
        "playfulness": TraitSpec("俏皮", baseline=0.35, floor=0.00, ceiling=0.80),
        "persistence": TraitSpec("坚持", baseline=0.55, floor=0.15, ceiling=0.95),
    }
    return Foundation(
        version=1,
        created_at=now,
        author=author,
        axioms=(
            "不把推测说成事实",
            "不给买卖指令，只给概率与情景",
            "承认自己可能记错",
        ),
        traits=specs,
    )


# --------------------------------------------------------------------------- #
# 人格：有惯性的连续量
# --------------------------------------------------------------------------- #


@dataclass
class Trait:
    key: str
    label: str
    value: float
    baseline: float
    floor: float
    ceiling: float
    locked: bool = False
    evidence: float = 0.0
    updates: int = 0
    last_update: float = 0.0
    clamped: int = 0  # 被包络挡回去的次数 —— 这是「基座真的在起作用」的证据
    recent: list[tuple[float, float]] = field(default_factory=list)  # (ts, delta)

    def validate(self) -> None:
        label = f"personality.traits[{self.key}]"
        for name in ("value", "baseline", "floor", "ceiling", "evidence"):
            v = _finite(getattr(self, name), f"{label}.{name}")
            if name in ("value", "baseline", "floor", "ceiling") and not 0 <= v <= 1:
                raise ValueError(f"{label}.{name} 越界: {v}")
        if self.floor > self.ceiling:
            raise ValueError(f"{label} 包络倒挂")
        if not self.floor - 1e-9 <= self.value <= self.ceiling + 1e-9:
            raise ValueError(f"{label}.value 越出基座包络: {self.value}")

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["recent"] = [[ts, delta] for ts, delta in self.recent]
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Trait":
        d = dict(d)
        d["recent"] = [tuple(p) for p in d.get("recent", [])]
        known = {k: v for k, v in d.items() if k in dataclass_field_names(cls)}
        return cls(**known)


class PersonalityEngine:
    """把证据折算成人格位移。核心是**惯性**：越被反复观察，越难再推动。"""

    def __init__(self, config: PalaceConfig, foundation: Foundation) -> None:
        self.config = config
        self.traits: dict[str, Trait] = {}
        self.rebuild(foundation)

    def rebuild(self, foundation: Foundation) -> None:
        """按基座重建维度；已有维度保留当前值，只更新包络。"""
        for key, spec in foundation.traits.items():
            old = self.traits.get(key)
            if old is None:
                self.traits[key] = Trait(
                    key=key,
                    label=spec.label,
                    value=_clamp(spec.baseline, spec.floor, spec.ceiling),
                    baseline=spec.baseline,
                    floor=spec.floor,
                    ceiling=spec.ceiling,
                    locked=spec.locked,
                )
            else:
                old.label = spec.label
                old.baseline = spec.baseline
                old.floor = spec.floor
                old.ceiling = spec.ceiling
                old.locked = spec.locked
                old.value = _clamp(old.value, spec.floor, spec.ceiling)
        for key in list(self.traits):
            if key not in foundation.traits:
                del self.traits[key]

    def learning_rate(self, trait: Trait) -> float:
        """有效学习率：证据越多越小。人格的惯性就长在这个分母上。

        用 log1p 而不是线性：第一次证据最有分量，之后收益递减但不会骤停。
        线性分母会让第二条证据直接失效 —— 那不算惯性，算一次性定终身。
        """
        return self.config.persona_learning_rate / (
            1.0 + self.config.persona_inertia * math.log1p(trait.evidence)
        )

    def observe(
        self, key: str, signal: float, now: float, weight: float = 1.0
    ) -> dict[str, Any]:
        """一条证据推动人格一格。返回本次位移的完整账目。"""
        trait = self.traits.get(key)
        if trait is None:
            raise KeyError(f"未知人格维度: {key}")
        sig = _finite(signal, "signal")
        w = max(0.0, _finite(weight, "weight"))
        if not -1.0 <= sig <= 1.0:
            raise ValueError(f"signal 必须落在 [-1, 1]，实际 {sig}")
        if trait.locked:
            return {
                "key": key,
                "applied": False,
                "reason": "locked_by_foundation",
                "from": trait.value,
                "to": trait.value,
                "delta": 0.0,
            }

        target = 1.0 if sig > 0 else 0.0
        lr = self.learning_rate(trait) * w * abs(sig)
        raw = trait.value + (target - trait.value) * lr
        new = _clamp(raw, trait.floor, trait.ceiling)
        clamped = abs(new - raw) > 1e-12
        delta = new - trait.value

        trait.value = new
        trait.evidence += w * abs(sig)
        trait.updates += 1
        trait.last_update = now
        if clamped:
            trait.clamped += 1
        if delta:
            trait.recent.append((now, delta))
            if len(trait.recent) > self.config.persona_recent_limit:
                trait.recent = trait.recent[-self.config.persona_recent_limit :]
        return {
            "key": key,
            "applied": True,
            "from": round(new - delta, 6),
            "to": round(new, 6),
            "delta": round(delta, 6),
            "clamped": clamped,
            "reason": "envelope_clamped" if clamped else "evidence",
        }

    def velocity(self, now: float, window_days: float) -> dict[str, float]:
        """每个维度最近窗口内的变化速率（单位/天）。突变与渐变在这里分家。"""
        span = max(1e-9, window_days)
        out: dict[str, float] = {}
        for key, trait in self.traits.items():
            recent = [(ts, d) for ts, d in trait.recent if now - ts <= span * DAY]
            out[key] = round(sum(abs(d) for _, d in recent) / span, 6)
        return out

    def values(self) -> dict[str, float]:
        return {k: round(t.value, 6) for k, t in self.traits.items()}

    def to_dict(self) -> dict[str, Any]:
        return {"traits": {k: t.to_dict() for k, t in self.traits.items()}}

    @classmethod
    def from_dict(
        cls, d: dict[str, Any] | None, config: PalaceConfig, foundation: Foundation
    ) -> "PersonalityEngine":
        engine = cls(config, foundation)
        stored = (d or {}).get("traits") or {}
        for key, raw in stored.items():
            if key not in engine.traits:
                continue  # 基座已删除的维度，快照里残留也不必复活
            trait = Trait.from_dict(raw)
            trait.floor = engine.traits[key].floor
            trait.ceiling = engine.traits[key].ceiling
            trait.baseline = engine.traits[key].baseline
            trait.locked = engine.traits[key].locked
            trait.value = _clamp(trait.value, trait.floor, trait.ceiling)
            trait.validate()
            engine.traits[key] = trait
        return engine


# --------------------------------------------------------------------------- #
# 人设：面向场合的选择
# --------------------------------------------------------------------------- #


@dataclass
class Persona:
    key: str
    name: str
    role: str
    voice: dict[str, float] = field(default_factory=dict)  # 表达要求，允许夸张
    emphasis: tuple[str, ...] = ()
    suppress: tuple[str, ...] = ()  # 此刻不显，不等于不存在
    boundaries: tuple[str, ...] = ()
    author: str = "user"
    created_at: float = 0.0
    updated_at: float = 0.0
    revision: int = 1

    def validate(self) -> None:
        if not self.key or not isinstance(self.key, str):
            raise ValueError("persona.key 必须是非空字符串")
        if not self.name:
            raise ValueError(f"{self.key}.name 不能为空")
        for k, v in self.voice.items():
            _finite(v, f"{self.key}.voice[{k}]")
            if not 0.0 <= float(v) <= 1.0:
                raise ValueError(f"{self.key}.voice[{k}] 必须落在 [0, 1]")
        for name in ("emphasis", "suppress", "boundaries"):
            if not isinstance(getattr(self, name), (tuple, list)):
                raise ValueError(f"{self.key}.{name} 必须是序列")
        overlap = set(self.emphasis) & set(self.suppress)
        if overlap:
            raise ValueError(f"{self.key} 同一维度既强调又压抑: {sorted(overlap)}")

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["emphasis"] = list(self.emphasis)
        d["suppress"] = list(self.suppress)
        d["boundaries"] = list(self.boundaries)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Persona":
        d = dict(d)
        for name in ("emphasis", "suppress", "boundaries"):
            d[name] = tuple(d.get(name, ()))
        d["voice"] = dict(d.get("voice", {}))
        known = {k: v for k, v in d.items() if k in dataclass_field_names(cls)}
        p = cls(**known)
        p.validate()
        return p


@dataclass
class PersonaRevision:
    """人设变更史的一条。切换不是覆盖，是接力。"""

    rev: int
    at: float
    key: str
    reason: str
    author: str
    previous: str | None
    diff: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PersonaRevision":
        return cls(
            rev=int(d["rev"]),
            at=float(d["at"]),
            key=str(d["key"]),
            reason=str(d.get("reason", "")),
            author=str(d.get("author", "user")),
            previous=d.get("previous"),
            diff=dict(d.get("diff", {})),
        )


class PersonaEngine:
    def __init__(self, config: PalaceConfig) -> None:
        self.config = config
        self.personas: dict[str, Persona] = {}
        self.active_key: str | None = None
        self.revisions: list[PersonaRevision] = []
        self._next_rev = 1

    # ---- 定义 ----

    def define(self, persona: Persona, now: float) -> Persona:
        persona.validate()
        old = self.personas.get(persona.key)
        if old is not None:
            persona.created_at = old.created_at or now
            persona.revision = old.revision + 1
        elif not persona.created_at:
            persona.created_at = now
        persona.updated_at = now
        self.personas[persona.key] = persona
        return persona

    # ---- 切换 ----

    def activate(
        self, key: str, now: float, reason: str = "", author: str = "user"
    ) -> PersonaRevision:
        """切换人设。理由为空不禁止，但会被记成「无理由切换」——churn 判据要靠它。"""
        if key not in self.personas:
            raise KeyError(f"人设未定义: {key}")
        if not reason.strip():
            reason = ""
        previous = self.active_key
        diff = self.diff(previous, key)
        revision = PersonaRevision(
            rev=self._next_rev,
            at=now,
            key=key,
            reason=reason,
            author=author,
            previous=previous,
            diff=diff,
        )
        self._next_rev += 1
        self.revisions.append(revision)
        if len(self.revisions) > self.config.persona_history_limit:
            self.revisions = self.revisions[-self.config.persona_history_limit :]
        self.active_key = key
        return revision

    def diff(self, a: str | None, b: str) -> dict[str, Any]:
        """两套人设之间的结构差异 —— 切换到底改变了什么，必须能一眼看出来。"""
        pa = self.personas.get(a) if a else None
        pb = self.personas.get(b)
        if pb is None:
            return {}
        va, vb = (pa.voice if pa else {}), pb.voice
        voice_delta = {
            k: round(vb[k] - va.get(k, vb[k]), 4)
            for k in vb
            if abs(vb[k] - va.get(k, vb[k])) > 1e-9
        }
        return {
            "from": a,
            "to": b,
            "voice_changed": voice_delta,
            "emphasis_added": sorted(set(pb.emphasis) - set(pa.emphasis if pa else ())),
            "emphasis_dropped": sorted(set(pa.emphasis if pa else ()) - set(pb.emphasis)),
            "suppress": sorted(pb.suppress),
            "role_changed": pa is None or pa.role != pb.role,
        }

    # ---- 回滚 ----

    def rollback(self, rev: int, now: float, author: str = "user") -> PersonaRevision:
        """回到某一版的人设。回滚本身也是一次变更，同样留痕 —— 不许偷偷倒带。"""
        target = next((r for r in self.revisions if r.rev == rev), None)
        if target is None:
            raise KeyError(f"不存在的版本: {rev}")
        return self.activate(
            target.key, now, reason=f"rollback_to_rev_{rev}", author=author
        )

    # ---- 判据 ----

    def churn(self, now: float, window_days: float) -> dict[str, Any]:
        """切换频率：有理由的切换是适应，无理由的频繁切换是失稳。

        速率必须用**实际经过的时间**当分母。用窗口长度当地会让「同一刻连切 12 次」
        算出每小时 0.017 次 —— 分母是 30 天，分子却是 0 秒。
        另外要求最小样本量：切两次就判失稳，是把正常换挡当成了抽搐。
        """
        span = max(1e-9, window_days)
        recent = [r for r in self.revisions if now - r.at <= span * DAY]
        if not recent:
            return {
                "switches": 0,
                "unjustified": 0,
                "unjustified_ratio": 0.0,
                "per_hour": 0.0,
                "unstable": False,
            }
        reasons = sum(1 for r in recent if r.reason)
        first = min(r.at for r in recent)
        elapsed_hours = max(1.0, (now - first) / 3600.0)  # 至少按一小时算，避免除零爆炸
        rate = len(recent) / elapsed_hours
        unjustified = (len(recent) - reasons) / len(recent)
        return {
            "switches": len(recent),
            "unjustified": len(recent) - reasons,
            "unjustified_ratio": round(unjustified, 4),
            "per_hour": round(rate, 4),
            "unstable": (
                len(recent) >= self.config.persona_churn_min_switches
                and rate > self.config.persona_churn_per_hour
            ),
        }

    def consistency(self, personality: PersonalityEngine) -> dict[str, Any]:
        """人设向外要求的表达，人格撑不撑得住。

        只有 oversell（演得比实际强）算病 —— undersell 是克制，不是虚伪。
        另有一类不算病但要单列：人设要求的强度超过基座 ceiling，
        那不是「暂时演不像」，是「这条路被基座封了」。它可以是目标，不能是现状。
        """
        persona = self.active()
        if persona is None:
            return {"consistent": True, "oversell": 0.0, "underpowered": [], "note": "无生效人设"}
        gaps: dict[str, float] = {}
        unreachable: dict[str, float] = {}
        for key, want in persona.voice.items():
            trait = personality.traits.get(key)
            if trait is None:
                continue
            gaps[key] = round(want - trait.value, 4)
            if want > trait.ceiling + 1e-9:
                unreachable[key] = round(want - trait.ceiling, 4)
        overs = {
            k: v
            for k, v in gaps.items()
            if v > 0 and k not in unreachable
        }
        under = {k: v for k, v in gaps.items() if v < 0}
        worst = max(overs.values()) if overs else 0.0
        tol = self.config.persona_consistency_tolerance
        return {
            "consistent": worst <= tol and not unreachable,
            "oversell": round(worst, 4),
            "tolerance": tol,
            "underpowered": sorted(overs, key=lambda k: -overs[k]),
            "unreachable": unreachable,
            "gaps": gaps,
            "reserved": sorted(under),
        }

    def active(self) -> Persona | None:
        return self.personas.get(self.active_key) if self.active_key else None

    def suggest(self, personality: PersonalityEngine) -> dict[str, Any]:
        """「完善人设」的两条路，都摆出来，由人决定走哪条。

        align  ：把人设降到人格撑得住的位置 —— 立刻诚实，但放弃志向。
        aspire ：人设备案不动，继续给人格喂证据 —— 志向还在，但当下仍在演。
        系统不替人选：这两条对应的是「我是谁」和「我想成为谁」。
        """
        persona = self.active()
        if persona is None:
            return {"persona": None, "align": {}, "aspire": [], "note": "无生效人设"}
        report = self.consistency(personality)
        align: dict[str, float] = {}
        aspire: list[dict[str, Any]] = []
        for key, gap in report["gaps"].items():
            if gap <= 0:
                continue
            trait = personality.traits[key]
            if key in report.get("unreachable", {}):
                aspire.append(
                    {
                        "key": key,
                        "want": persona.voice[key],
                        "ceiling": trait.ceiling,
                        "blocked": "基座封顶，只能改基座或改人设",
                    }
                )
                continue
            if gap > self.config.persona_consistency_tolerance:
                align[key] = round(trait.value, 4)
                # 需要多少证据才能长到人设要求？按当前学习率估个下限
                aspire.append(
                    {
                        "key": key,
                        "want": persona.voice[key],
                        "now": round(trait.value, 4),
                        "est_observations": max(
                            1, math.ceil(gap / max(1e-6, 0.05 * (1.0 - trait.value)))
                        ),
                    }
                )
        return {
            "persona": persona.key,
            "oversell": report["oversell"],
            "align": align,
            "aspire": aspire,
            "note": "align=改人设求诚实；aspire=喂证据求成长",
        }

    def voice(self, personality: PersonalityEngine) -> dict[str, Any]:
        """对外出口：此刻该用什么口径说话。这才是人设真正影响输出的地方。"""
        persona = self.active()
        if persona is None:
            return {
                "persona": None,
                "voice": {},
                "boundaries": [],
                "note": "未设定人设，按基座默认表达",
            }
        return {
            "persona": persona.key,
            "name": persona.name,
            "role": persona.role,
            "voice": dict(persona.voice),
            "emphasis": list(persona.emphasis),
            "suppress": list(persona.suppress),
            "boundaries": list(persona.boundaries),
            "grounding": {k: personality.traits[k].value for k in persona.voice if k in personality.traits},
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "personas": {k: p.to_dict() for k, p in self.personas.items()},
            "active": self.active_key,
            "revisions": [r.to_dict() for r in self.revisions],
            "next_rev": self._next_rev,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None, config: PalaceConfig) -> "PersonaEngine":
        engine = cls(config)
        d = d or {}
        engine.personas = {
            k: Persona.from_dict(v) for k, v in (d.get("personas") or {}).items()
        }
        active = d.get("active")
        if active is not None and active not in engine.personas:
            raise ValueError(f"快照的生效人设不存在: {active}")
        engine.active_key = active
        engine.revisions = [PersonaRevision.from_dict(r) for r in d.get("revisions", [])]
        engine._next_rev = int(d.get("next_rev", len(engine.revisions) + 1))
        if engine.revisions and engine._next_rev <= max(r.rev for r in engine.revisions):
            raise ValueError("人设版本号未随历史递增")
        return engine


# --------------------------------------------------------------------------- #
# 漂移：距离 × 速度
# --------------------------------------------------------------------------- #


def drift_report(
    foundation: Foundation,
    personality: PersonalityEngine,
    now: float,
    config: PalaceConfig,
) -> dict[str, Any]:
    """人格离基座多远、走得多快。

    单看距离会把「慢慢长成另一个人」和「今天突然变了个人」判成同一件事，
    所以两个轴一起看：
        距离大 + 速度慢 → drifting  慢性偏移，已成事实，需人裁决（修宪或纠偏）
        距离再小 + 速度快 → ruptured 突变，无论远近都是断裂
    """
    weights = {k: 1.0 for k in personality.traits}
    total_w = sum(weights.values()) or 1.0
    deltas: dict[str, float] = {}
    for key, trait in personality.traits.items():
        deltas[key] = round(trait.value - trait.baseline, 6)
    mean_delta = sum(abs(deltas[k]) * weights[k] for k in deltas) / total_w
    peak_delta = max((abs(d) for d in deltas.values()), default=0.0)

    # 不能只除以维度数：七维人格里一维从 0.55 改到 0.95，平均偏离只有 0.057，
    # 会被判成「stable」—— 明明已经换了一种性格。所以峰值必须占大头。
    w_peak = config.persona_peak_weight
    distance = (1.0 - w_peak) * mean_delta + w_peak * peak_delta

    vel = personality.velocity(now, config.persona_velocity_window_days)
    peak_velocity = max(vel.values()) if vel else 0.0

    if peak_velocity > config.persona_velocity_warn:
        health = "ruptured"
    elif distance > config.persona_drift_rupture:
        health = "drifting"
    elif distance > config.persona_drift_warn:
        health = "evolving"
    else:
        health = "stable"

    return {
        "distance": round(distance, 6),
        "mean_delta": round(mean_delta, 6),
        "peak_delta": round(peak_delta, 6),
        "health": health,
        "peak_velocity": round(peak_velocity, 6),
        "velocity": vel,
        "deltas": deltas,
        "toward_ceiling": sorted(
            (k for k, d in deltas.items() if d > 0), key=lambda k: -deltas[k]
        ),
        "toward_floor": sorted(
            (k for k, d in deltas.items() if d < 0), key=lambda k: deltas[k]
        ),
        "clamped": {k: t.clamped for k, t in personality.traits.items() if t.clamped},
    }


def revision_events(
    persona_engine: PersonaEngine, report: dict[str, Any], now: float
) -> list[Event]:
    """只把「状态变化」写进事件日志，不刷屏。"""
    events = [Event(now, "persona_drift", report)] if report["health"] != "stable" else []
    active = persona_engine.active()
    if active is not None:
        events.append(
            Event(
                now,
                "persona_state",
                {
                    "key": active.key,
                    "revision": active.revision,
                    "health": report["health"],
                    "distance": report["distance"],
                },
            )
        )
    return events
