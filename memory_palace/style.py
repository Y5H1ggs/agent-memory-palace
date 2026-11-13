"""style.py - 风格固化与冲突裁决。

固化：证据要够多、够可信、且跨越时间；反证可撤销固化。
裁决：对立的固化条目不得同时进入 active_style。
      · 用户显式钉住的优先；
      · 否则按证据强度择一；
      · 差距落在容差带内则判定「悬而未决」，两条都停用，不硬选一个；
      · 裁决不是终审判决：证据反转会自动翻转，全过程留可追溯记录。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .core import DAY, Event, PalaceConfig, dataclass_field_names


@dataclass
class Adjudication:
    ts: float
    pair: tuple[str, str]
    winner: str | None
    reason: str
    margin: float
    confidence: tuple[float, float]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["pair"] = list(self.pair)
        d["confidence"] = list(self.confidence)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Adjudication":
        return cls(
            ts=float(d["ts"]),
            pair=tuple(d["pair"]),
            winner=d.get("winner"),
            reason=str(d["reason"]),
            margin=float(d["margin"]),
            confidence=tuple(d.get("confidence", (0.0, 0.0))),
        )


@dataclass
class StyleRule:
    key: str
    statement: str
    support: float = 0.0
    contradict: float = 0.0
    observations: int = 0
    first_seen: float = 0.0
    last_evidence: float = 0.0
    crystallized: bool = False
    pinned_by_user: bool = False
    opposes: tuple[str, ...] = ()
    flip_count: int = 0
    suppressed_by: str | None = None
    adjudications: int = 0

    def decayed(self, now: float, half_life_days: float) -> tuple[float, float]:
        """纯函数：证据按半衰期衰减，读数不写回账本。"""
        age = max(0.0, (now - self.last_evidence) / DAY)
        k = 0.5 ** (age / max(1e-9, half_life_days))
        return self.support * k, self.contradict * k

    def confidence(self, now: float, half_life_days: float) -> float:
        s, c = self.decayed(now, half_life_days)
        return s / (s + c + 1.0)

    def settle(self, now: float, half_life_days: float) -> None:
        """只在接收新证据时搬动锚点，避免重复结算。"""
        self.support, self.contradict = self.decayed(now, half_life_days)
        self.last_evidence = now

    def observe(
        self, signal: float, weight: float, now: float, half_life_days: float
    ) -> None:
        self.settle(now, half_life_days)
        amount = max(0.0, weight)
        if signal >= 0:
            self.support += amount
        else:
            self.contradict += amount
        self.observations += 1
        self.last_evidence = now
        if self.first_seen <= 0:
            self.first_seen = now

    def span_days(self) -> float:
        return max(0.0, (self.last_evidence - self.first_seen) / DAY)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["opposes"] = list(self.opposes)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "StyleRule":
        d = dict(d)
        d["opposes"] = tuple(d.get("opposes", ()))
        known = {k: v for k, v in d.items() if k in dataclass_field_names(cls)}
        return cls(**known)


class StyleEngine:
    def __init__(self, config: PalaceConfig) -> None:
        self.config = config
        self.rules: dict[str, StyleRule] = {}
        self.adjudication_log: list[Adjudication] = []

    def observe(
        self,
        key: str,
        statement: str,
        signal: float,
        now: float,
        weight: float = 1.0,
        opposes: tuple[str, ...] = (),
        pinned_by_user: bool = False,
    ) -> StyleRule:
        rule = self.rules.get(key)
        if rule is None:
            rule = StyleRule(
                key=key,
                statement=statement,
                first_seen=now,
                last_evidence=now,
                opposes=tuple(opposes),
            )
            self.rules[key] = rule
        if statement:
            rule.statement = statement
        if opposes:
            rule.opposes = tuple(dict.fromkeys((*rule.opposes, *opposes)))
        if pinned_by_user:
            rule.pinned_by_user = True
        rule.observe(signal, weight, now, self.config.style_half_life_days)
        self.adjudicate(now, emit=False)  # 保持「对立不并存」为不变量，不依赖调用者记得跑周期
        return rule

    def strength(self, rule: StyleRule, now: float) -> float:
        """证据强度：支持量 × 置信度，并因历史反复翻转而打折。"""
        hl = self.config.style_half_life_days
        support = rule.decayed(now, hl)[0]
        return support * rule.confidence(now, hl) / (1.0 + 0.5 * rule.flip_count)

    def adjudicate(self, now: float, emit: bool = True) -> list[Event]:
        """重新裁决所有对立对；每轮从零开始，避免旧判决僵化。

        不是简单「谁大听谁」：差距落在容差带内说明现有证据分不出高下，
        这时硬选一个就是把噪声固化成了风格，因此两条一起停用。
        """
        hl = self.config.style_half_life_days
        tolerance = self.config.style_adjudication_margin
        for rule in self.rules.values():
            rule.suppressed_by = None

        events: list[Event] = []
        # 先算全部对局，再统一落抑制标记：否则 A>B、A<C 这种链条会随遍历顺序摇摆。
        losers: dict[str, str] = {}

        for a_key, b_key in self.conflicts(now):
            a, b = self.rules[a_key], self.rules[b_key]
            if not (self.is_effective(a, now) and self.is_effective(b, now)):
                continue  # 只有一边真正在输出，算不上冲突，不必惊动裁决
            ca, cb = a.confidence(now, hl), b.confidence(now, hl)
            sa, sb = self.strength(a, now), self.strength(b, now)
            margin = abs(sa - sb) / max(sa, sb, 1e-9)

            if a.pinned_by_user and not b.pinned_by_user:
                winner, reason = a, "user_pinned"
            elif b.pinned_by_user and not a.pinned_by_user:
                winner, reason = b, "user_pinned"
            elif a.pinned_by_user and b.pinned_by_user:
                # 两个都被用户钉死却互相矛盾：机器无权替人裁决，双双停用并大声上报。
                winner, reason = None, "user_conflict"
            elif margin < tolerance:
                winner, reason = None, "ambiguous"
            else:
                winner, reason = (a if sa > sb else b), "evidence_strength"

            if winner is None:
                losers[a_key] = losers[b_key] = reason
            else:
                loser = b if winner is a else a
                losers[loser.key] = winner.key
            a.adjudications += 1
            b.adjudications += 1

            record = Adjudication(
                ts=now,
                pair=(a_key, b_key),
                winner=winner.key if winner else None,
                reason=reason,
                margin=round(margin, 6),
                confidence=(round(ca, 4), round(cb, 4)),
            )
            self.adjudication_log.append(record)
            if emit:
                events.append(Event(now, "adjudicate", record.to_dict()))

        for key, by in losers.items():
            rule = self.rules.get(key)
            if rule is not None:
                rule.suppressed_by = by
        if len(self.adjudication_log) > 2000:
            self.adjudication_log = self.adjudication_log[-2000:]
        return events

    def run_cycle(self, now: float) -> list[Event]:
        cfg = self.config
        hl = cfg.style_half_life_days
        events: list[Event] = []
        for rule in self.rules.values():
            conf = rule.confidence(now, hl)
            payload = {
                "key": rule.key,
                "confidence": round(conf, 4),
                "observations": rule.observations,
                "span_days": round(rule.span_days(), 2),
            }
            if rule.pinned_by_user:
                if not rule.crystallized:
                    rule.crystallized = True
                    payload["reason"] = "user_pinned"
                    events.append(Event(now, "crystallize", payload))
                continue
            if not rule.crystallized:
                if (
                    conf >= cfg.style_crystallize_confidence
                    and rule.observations >= cfg.style_min_observations
                    and rule.span_days() >= cfg.style_min_span_days
                ):
                    rule.crystallized = True
                    payload["reason"] = "evidence_crystallized"
                    events.append(Event(now, "crystallize", payload))
            elif conf < cfg.style_decrystallize_confidence:
                rule.crystallized = False
                rule.flip_count += 1
                payload["reason"] = "contradicted"
                events.append(Event(now, "decrystallize", payload))
        events.extend(self.adjudicate(now, emit=True))
        return events

    def is_effective(self, rule: StyleRule, now: float) -> bool:
        """是否处在「会影响输出」的状态：未被抑制，且（用户钉死 或 已固化且置信度过线）。"""
        if rule.suppressed_by is not None:
            return False
        if rule.pinned_by_user:
            return True
        return (
            rule.crystallized
            and rule.confidence(now, self.config.style_half_life_days) >= 0.5
        )

    def active_statements(self, now: float) -> list[str]:
        """风格固化的落地点：只有真正生效且未被裁决抑制的条目才能影响输出。"""
        return [r.statement for r in self.rules.values() if self.is_effective(r, now)]

    def conflicts(self, now: float) -> list[tuple[str, str]]:
        """结构性对立：双方都够格参与风格（已固化或用户钉死），与是否被抑制无关。

        与 active_conflicts 的区别很重要：裁决之后对立方仍应被看见 —— 否则
        「冲突消失了」和「冲突被压下去了」在报表上长得一模一样。
        """
        eligible = {
            r.key
            for r in self.rules.values()
            if r.crystallized or r.pinned_by_user
        }
        found: list[tuple[str, str]] = []
        seen: set[frozenset[str]] = set()
        for rule in self.rules.values():
            if rule.key not in eligible:
                continue
            for other_key in rule.opposes:
                if other_key not in eligible:
                    continue
                key = frozenset((rule.key, other_key))
                if key in seen:
                    continue
                seen.add(key)
                found.append(tuple(sorted((rule.key, other_key))))
        found.sort()
        return found

    def active_conflicts(self, now: float) -> list[tuple[str, str]]:
        """已经真正同时生效的冲突对 —— 裁决正确时应恒为空。"""
        return [
            (a, b)
            for a, b in self.conflicts(now)
            if self.is_effective(self.rules[a], now)
            and self.is_effective(self.rules[b], now)
        ]

    def pending_conflicts(self, now: float) -> list[dict[str, Any]]:
        """已判定但未被证据解决的对立对：悬而未决 / 用户自相矛盾。"""
        out = []
        for rule in self.rules.values():
            if rule.suppressed_by in ("ambiguous", "user_conflict"):
                out.append(
                    {
                        "key": rule.key,
                        "reason": rule.suppressed_by,
                        "statement": rule.statement,
                    }
                )
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "rules": [r.to_dict() for r in self.rules.values()],
            "adjudications": [a.to_dict() for a in self.adjudication_log],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any], config: PalaceConfig) -> "StyleEngine":
        engine = cls(config)
        engine.rules = {
            r["key"]: StyleRule.from_dict(r) for r in d.get("rules", [])
        }
        engine.adjudication_log = [
            Adjudication.from_dict(a) for a in d.get("adjudications", [])
        ]
        return engine
