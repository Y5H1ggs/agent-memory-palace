"""test_guardrails.py - 自修正的多目标护栏：主指标变好，但不许拿别的指标去换。

这条测试针对的批评是「自修正的『变好』仍是单指标」。
单指标优化在真实系统里必然挤占别的指标，所以判据必须有第二道闸。
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from memory_palace.core import MutableClock, PalaceConfig  # noqa: E402
from memory_palace.selfcorrect import (  # noqa: E402
    ALLOWED_PARAMS,
    GUARD_DIRECTIONS,
    Candidate,
    SelfCorrector,
)


def _apply_one(corrector: SelfCorrector, metrics: dict, now: float):
    cand = Candidate(
        code="test_signal",
        param="semantic_min_score",
        direction="-",
        reason="覆盖率低",
        metric="retrieval_coverage",
        better="higher",
    )
    patches = corrector.propose([cand], metrics, now)
    assert len(patches) == 1, patches
    assert corrector.apply(patches[0])
    return patches[0]


def test_patch_records_guard_baseline() -> None:
    corrector = SelfCorrector(PalaceConfig())
    metrics = {
        "retrieval_coverage": 0.8,
        "forget_pressure": 0.2,
        "loose_fragments": 12,
        "consolidations": 3,
    }
    patch = _apply_one(corrector, metrics, 1000.0)
    assert patch.guards == {
        "retrieval_coverage": 0.8,
        "forget_pressure": 0.2,
        "loose_fragments": 12.0,
        "consolidations": 3.0,
    }


def test_improvement_with_guardrail_sacrifice_is_reverted() -> None:
    """主指标从 0.5 涨到 0.9（大幅改善），但遗忘压力从 0.2 涨到 0.9 —— 必须退回。"""
    config = PalaceConfig(selfcorrect_verify_hours=1.0, selfcorrect_guard_margin=0.15)
    corrector = SelfCorrector(config)
    base = {"retrieval_coverage": 0.5, "forget_pressure": 0.2, "loose_fragments": 10}
    patch = _apply_one(corrector, base, 0.0)
    after = {"retrieval_coverage": 0.9, "forget_pressure": 0.9, "loose_fragments": 10}
    settled = corrector.verify(after, now=7200.0, metrics_at=7200.0)
    assert settled and settled[0].status == "reverted", settled
    assert settled[0].guard_hits.get("forget_pressure") is not None
    assert "guardrail" in settled[0].evidence
    assert config.semantic_min_score == patch.before, "被退回的修改必须真的还原"


def test_improvement_without_sacrifice_is_kept() -> None:
    config = PalaceConfig(selfcorrect_verify_hours=1.0, selfcorrect_guard_margin=0.15)
    corrector = SelfCorrector(config)
    base = {"retrieval_coverage": 0.5, "forget_pressure": 0.2}
    patch = _apply_one(corrector, base, 0.0)
    after = {"retrieval_coverage": 0.85, "forget_pressure": 0.21}
    settled = corrector.verify(after, now=7200.0, metrics_at=7200.0)
    assert settled and settled[0].status == "kept", settled
    assert config.semantic_min_score == patch.after


def test_guard_direction_matters() -> None:
    """方向弄反会得到「越改越差才留下」的系统 —— 这比没有护栏更糟。"""
    config = PalaceConfig(selfcorrect_verify_hours=1.0, selfcorrect_guard_margin=0.15)
    corrector = SelfCorrector(config)
    base = {"retrieval_coverage": 0.5, "consolidations": 10}
    _apply_one(corrector, base, 0.0)
    # consolidations 越高越好：涨到 20 不该被判成牺牲
    after = {"retrieval_coverage": 0.85, "consolidations": 20}
    settled = corrector.verify(after, now=7200.0, metrics_at=7200.0)
    assert settled[0].status == "kept", settled
    # loose_fragments 越低越好：涨上去要退回
    corrector2 = SelfCorrector(PalaceConfig(selfcorrect_verify_hours=1.0))
    base2 = {"retrieval_coverage": 0.5, "loose_fragments": 10}
    _apply_one(corrector2, base2, 0.0)
    settled2 = corrector2.verify(
        {"retrieval_coverage": 0.9, "loose_fragments": 30}, now=7200.0, metrics_at=7200.0
    )
    assert settled2[0].status == "reverted", settled2


def test_small_guardrail_drift_is_tolerated() -> None:
    """护栏是防「拿 A 换 B」，不是防正常抖动：5% 的波动不许触发回滚。"""
    config = PalaceConfig(selfcorrect_verify_hours=1.0, selfcorrect_guard_margin=0.15)
    corrector = SelfCorrector(config)
    _apply_one(corrector, {"retrieval_coverage": 0.5, "forget_pressure": 0.20}, 0.0)
    settled = corrector.verify(
        {"retrieval_coverage": 0.9, "forget_pressure": 0.21}, now=7200.0, metrics_at=7200.0
    )
    assert settled[0].status == "kept"


def test_status_exposes_guardrail_evidence() -> None:
    config = PalaceConfig(selfcorrect_verify_hours=1.0)
    corrector = SelfCorrector(config)
    _apply_one(corrector, {"retrieval_coverage": 0.5, "forget_pressure": 0.2}, 0.0)
    corrector.verify(
        {"retrieval_coverage": 0.9, "forget_pressure": 0.9}, now=7200.0, metrics_at=7200.0
    )
    status = corrector.status()
    assert status["guardrail_reverts"], status
    assert status["guard_directions"] == GUARD_DIRECTIONS
    assert status["guard_margin"] == config.selfcorrect_guard_margin


def test_guard_metrics_are_real_reflection_metrics() -> None:
    """护栏指标必须是反思真的会产出的键，否则护栏永远不触发（等于没有）。"""
    from memory_palace.core import MemoryGraph
    from memory_palace.reflection import ReflectionEngine
    from memory_palace.style import StyleEngine

    engine = ReflectionEngine(PalaceConfig())
    report = engine.run(
        MemoryGraph(), StyleEngine(PalaceConfig()), [], {}, 1_800_000_000.0
    )
    keys = set(report.metrics)
    overlap = keys & set(GUARD_DIRECTIONS)
    assert overlap, f"护栏指标 {sorted(GUARD_DIRECTIONS)} 与反思指标 {sorted(keys)} 没有交集"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  [PASS] {name}")
    print("\n多目标护栏自测通过 ✅")
