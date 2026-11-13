"""affect.py - 情感权重：记忆里那个「疼」或「亮」的分量。

三个不同的量，以前被压成了一个 `base_weight`：
  · 现在还在多大程度上「感到」   → salience(now)，**会退潮**
  · 当时有多强烈                 → peak_arousal，**永不衰减的刻痕**
  · 因为强烈而有多不可靠         → fidelity，**越情绪化越可能是错的**

第一条是潮水，第二条是河床，第三条是这个模型里最不讨好的部分：
情绪让记忆更难消失，同时也让它更容易失真。只给前者不给后者，就是在骗自己。

第四件事（本版新增）：**这两个数从哪来**。
以前 valence / arousal 全靠调用方手填，系统自己不会产生情绪 —— 那不是情感模型，
是情感参数。于是补一个评价器（appraisal）：
  · 文本里明确的情绪词（痛 / 亏 / 突破 / 恐慌…）→ 效价 + 唤醒
  · 程度副词（极 / 严重 / 彻底…）→ 只放大唤醒，不改效价符号
  · 运行时结果（止损兑现 / 记忆误删 / 逻辑被反驳…）→ 反馈式情绪
手填永远优先，且**评价器只解释、不覆盖**：它给出 drivers（命中了哪些词），
好让「这条记忆为什么发烫」能被追问，而不是凭空出现一个 0.95。
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any

DAY = 86400.0

# 情绪词表刻意收窄：只收「几乎不可能是中性叙述」的词。
# 宽词表会让「记录」「复盘」这类中性词带上情绪，整套权重就悄悄漂了。
APPRAISAL_LEXICON: dict[str, tuple[float, float]] = {
    "痛": (-0.75, 0.70),
    "疼": (-0.70, 0.65),
    "亏": (-0.70, 0.70),
    "亏损": (-0.80, 0.75),
    "爆亏": (-1.00, 0.95),
    "重挫": (-0.95, 0.90),
    "暴跌": (-0.90, 0.85),
    "崩": (-0.90, 0.90),
    "踏空": (-0.60, 0.70),
    "后悔": (-0.65, 0.60),
    "失败": (-0.70, 0.70),
    "错误": (-0.45, 0.45),
    "错了": (-0.60, 0.60),
    "危险": (-0.60, 0.80),
    "恐慌": (-0.85, 0.90),
    "焦虑": (-0.70, 0.75),
    "失望": (-0.65, 0.55),
    "害怕": (-0.75, 0.80),
    "赚": (0.65, 0.65),
    "盈利": (0.75, 0.70),
    "成功": (0.80, 0.75),
    "突破": (0.70, 0.80),
    "兑现": (0.60, 0.60),
    "安心": (0.50, 0.20),
    "满意": (0.55, 0.30),
    "庆幸": (0.50, 0.50),
}

INTENSIFIERS: tuple[str, ...] = ("极其", "非常", "特别", "严重", "彻底", "疯狂", "爆")

# 运行时结果 → 情绪。这是「感受一次」的来源，与「回想一次」严格分开。
OUTCOMES: dict[str, tuple[float, float, str]] = {
    "loss": (-0.70, 0.75, "止损兑现：结果不利"),
    "win": (0.70, 0.70, "结果有利"),
    "error": (-0.55, 0.60, "做错了"),
    "contradicted": (-0.50, 0.65, "被反证推翻"),
    "confirmed": (0.40, 0.35, "被证据支持"),
    "missed": (-0.35, 0.50, "想不起来"),
    "recalled": (0.15, 0.25, "顺利想起"),
}


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


@dataclass
class Appraisal:
    """一次评价的结果：两个数 + 命中依据。没有依据的情绪，不允许进系统。"""

    valence: float = 0.0
    arousal: float = 0.0
    source: str = "neutral"  # explicit / text / outcome / neutral
    drivers: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "valence": round(self.valence, 6),
            "arousal": round(self.arousal, 6),
            "source": self.source,
            "drivers": list(self.drivers),
        }


def appraise_text(text: str) -> Appraisal:
    """从文本读出情绪。同号多项按「最强 + 其余折半」合成，避免词多就情绪爆炸。"""
    source = (text or "").lower()
    hits: list[tuple[str, float, float]] = []
    for term, (v, a) in APPRAISAL_LEXICON.items():
        if term in source:
            hits.append((term, v, a))
    if not hits:
        return Appraisal()
    # 只取每一极的最强者，另一极按 0.35 计入 —— 「亏了但学到东西」不该被判成狂喜。
    negatives = sorted((h for h in hits if h[1] < 0), key=lambda h: h[1])
    positives = sorted((h for h in hits if h[1] > 0), key=lambda h: -h[1])
    strongest = negatives[0] if negatives else positives[0]
    sign = -1.0 if strongest[1] < 0 else 1.0
    same = negatives if sign < 0 else positives
    other = positives if sign < 0 else negatives
    magnitude = abs(same[0][1]) + 0.35 * sum(abs(h[1]) for h in same[1:])
    if other:
        magnitude -= 0.25 * abs(other[0][1])
    valence = _clamp(sign * magnitude, -1.0, 1.0)
    arousal = max(h[2] for h in same)
    boosts = sum(1 for word in INTENSIFIERS if word in source)
    arousal = _clamp(arousal * (1.0 + 0.15 * boosts), 0.0, 1.0)
    drivers = tuple(h[0] for h in same[:3]) + tuple(
        f"!{word}" for word in INTENSIFIERS if word in source
    )
    return Appraisal(
        valence=valence, arousal=arousal, source="text", drivers=drivers
    )


@dataclass
class Affect:
    """一条记忆的情绪档案。半衰期随记录一起走，是快照的一部分而不是全局状态。"""

    valence: float = 0.0       # [-1, 1] 效价：负=痛，正=亮
    arousal: float = 0.0       # [0, 1] 唤醒度：冷却值，会随时间退潮
    peak_arousal: float = 0.0  # [0, 1] 编码期峰值，永不衰减
    felt_at: float = 0.0       # 最近一次情绪写入的时刻
    arousal_half_life: float = 3.0
    valence_half_life: float = 10.0
    # 这两个数从哪来：手填 / 文本评价 / 运行时结果 / 中性。不可追溯的情绪不参与审计。
    source: str = "neutral"
    drivers: tuple[str, ...] = ()

    def settled(self, now: float) -> tuple[float, float]:
        """纯函数：把「当时」的情绪折算到「现在」。调用 N 次结果完全一致。"""
        if self.felt_at <= 0.0:
            return self.valence, self.arousal
        days = max(0.0, (now - self.felt_at) / DAY)
        valence = self.valence * 0.5 ** (days / self.valence_half_life)
        arousal = self.arousal * 0.5 ** (days / self.arousal_half_life)
        return valence, arousal

    def salience(self, now: float) -> float:
        """现在还能感到多强：情绪的「在场感」。唤醒度起着放大器的作用。"""
        valence, arousal = self.settled(now)
        return abs(valence) * (0.4 + 0.6 * arousal)

    def residue(self, now: float) -> float:
        """潮水退去后留下的印记：当下感受为主，刻痕为辅。"""
        return _clamp(0.75 * self.salience(now) + 0.25 * self.peak_arousal, 0.0, 1.0)

    def stability_bonus(self, now: float, gain: float) -> float:
        """情绪让记忆更顽固。这一项直接乘进稳定性，所以它救命，也所以它危险。"""
        return _clamp(gain * (0.6 * self.salience(now) + 0.4 * self.peak_arousal), 0.0, gain)

    def fidelity(self, distortion: float) -> float:
        """可靠性：唤醒越强，编码越容易被情绪改写。这条**不随时间恢复**。"""
        return _clamp(1.0 - distortion * self.peak_arousal, 0.0, 1.0)

    def validate(self, name: str = "affect") -> None:
        for key in ("valence", "arousal", "peak_arousal", "felt_at"):
            value = getattr(self, key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name}.{key} 类型非法: {value!r}")
            if not math.isfinite(float(value)):
                raise ValueError(f"{name}.{key} 不是有限数: {value!r}")
        if not -1.0 <= self.valence <= 1.0:
            raise ValueError(f"{name}.valence 必须落在 [-1, 1]，实际 {self.valence}")
        for key in ("arousal", "peak_arousal"):
            if not 0.0 <= getattr(self, key) <= 1.0:
                raise ValueError(f"{name}.{key} 必须落在 [0, 1]，实际 {getattr(self, key)}")
        if self.peak_arousal + 1e-9 < self.arousal:
            raise ValueError(f"{name}.peak_arousal 不得低于当前 arousal")
        for key in ("arousal_half_life", "valence_half_life"):
            value = getattr(self, key)
            if not math.isfinite(float(value)) or value <= 0:
                raise ValueError(f"{name}.{key} 必须为正数: {value!r}")
        if self.source not in ("explicit", "text", "outcome", "neutral"):
            raise ValueError(f"{name}.source 未知: {self.source!r}")
        if not isinstance(self.drivers, (tuple, list)):
            raise ValueError(f"{name}.drivers 类型非法")

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["drivers"] = list(self.drivers)
        return data

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "Affect":
        if not d:
            return cls()
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        if "drivers" in known:
            known["drivers"] = tuple(known["drivers"] or ())
        affect = cls(**known)
        affect.validate()
        return affect


class AffectEngine:
    """情绪写入与重估。两条铁律：

    1. 写情绪**不等于**强化记忆 —— 这里绝不碰 base_weight / last_accessed。
       否则「回想一次」和「感受一次」被混为一谈，账本立刻失真。
    2. 先结算再叠加 —— 同一股情绪重复写入必须收敛，不能反复计息。
    """

    def __init__(self, config: Any) -> None:
        self.config = config

    def blank(self, now: float) -> Affect:
        return Affect(
            felt_at=now,
            arousal_half_life=self.config.affect_arousal_half_life_days,
            valence_half_life=self.config.affect_valence_half_life_days,
        )

    def feel(self, affect: Affect, now: float, valence: float, arousal: float) -> Affect:
        if not -1.0 <= valence <= 1.0:
            raise ValueError(f"valence 必须落在 [-1, 1]，实际 {valence}")
        if not 0.0 <= arousal <= 1.0:
            raise ValueError(f"arousal 必须落在 [0, 1]，实际 {arousal}")
        cur_v, cur_a = affect.settled(now)
        affect.valence = _clamp(0.5 * cur_v + 0.5 * valence, -1.0, 1.0)
        affect.arousal = _clamp(max(0.5 * cur_a, arousal), 0.0, 1.0)
        affect.peak_arousal = max(affect.peak_arousal, arousal, affect.arousal)
        affect.felt_at = now
        affect.arousal_half_life = self.config.affect_arousal_half_life_days
        affect.valence_half_life = self.config.affect_valence_half_life_days
        affect.validate()
        return affect

    # ---- 情绪从哪来：评价，而不是拍脑袋 ----

    def appraise(self, text: str) -> Appraisal:
        """内容 → 情绪。

        刻意**不看** tier / pinned / 标签：一条钉死的硬规则并不「情绪激动」，
        把结构性重要性折成唤醒度，会让「可靠度」被无关的东西污染。
        结构性的情绪只能从运行时结果来（见 feel_outcome）。
        """
        return appraise_text(text)

    def feel_appraisal(
        self, affect: Affect, now: float, appraisal: Appraisal
    ) -> Affect:
        """把评价结果写进情绪档案，并留下依据。零情绪不写入（否则 felt_at 被无意义重置）。"""
        if appraisal.valence == 0.0 and appraisal.arousal == 0.0:
            affect.source = "neutral"
            return affect
        self.feel(affect, now, appraisal.valence, appraisal.arousal)
        affect.source = appraisal.source
        affect.drivers = tuple(appraisal.drivers)
        return affect

    def feel_outcome(self, affect: Affect, now: float, outcome: str) -> Affect:
        """运行时结果反馈：这是「感受一次」的合法来源，但绝不强化记忆本身。"""
        if outcome not in OUTCOMES:
            raise ValueError(f"未知结果类型: {outcome!r}（可选 {sorted(OUTCOMES)}）")
        valence, arousal, label = OUTCOMES[outcome]
        return self.feel_appraisal(
            affect,
            now,
            Appraisal(
                valence=valence,
                arousal=arousal,
                source="outcome",
                drivers=(f"结果:{outcome}", label),
            ),
        )

    def reappraise(self, affect: Affect, now: float, valence: float) -> Affect:
        """情绪重估：改写「现在怎么看这件事」，不动事实、不动强度、不动峰痕。

        这是心理治疗里唯一有实证的机制，也是记忆系统里唯一允许「改感受不改内容」的入口。
        """
        if not -1.0 <= valence <= 1.0:
            raise ValueError(f"valence 必须落在 [-1, 1]，实际 {valence}")
        cur_v, cur_a = affect.settled(now)
        affect.valence = _clamp(valence, -1.0, 1.0)
        affect.arousal = cur_a
        affect.felt_at = now  # 从此刻重新开始退潮
        affect.validate()
        return affect
