"""test_llm.py - LLM 反思的边界：结构化、可降级、绝不改配置。

这里测的不是「模型说得对不对」（那要人看），而是三件代码必须保证的事：
  1. 输出必须是结构化 JSON，坏输出要降级而不是被当成反思
  2. 白名单外的参数一律丢弃
  3. 无论模型说什么，配置都不许变
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from memory_palace.core import MutableClock, PalaceConfig  # noqa: E402
from memory_palace.llm import (  # noqa: E402
    CallableProvider,
    LLMReflector,
    SYSTEM_PROMPT,
    make_reflector,
)
from memory_palace.palace import MemoryPalace  # noqa: E402
from memory_palace.selfcorrect import ALLOWED_PARAMS  # noqa: E402

GOOD = {
    "findings": ["最近 7 天召回率从 0.8 掉到 0.55", "沉淀次数为 0，碎片在堆积"],
    "hypotheses": ["语义门槛偏高，短查询被弃答"],
    "suggested_params": [
        {"param": "semantic_min_score", "direction": "down", "why": "误弃率上升"},
        {"param": "forget_w", "direction": "up", "why": "越权尝试"},
        {"param": "nonexistent", "direction": "down", "why": "编造"},
    ],
    "confidence": 0.72,
}


def _payload() -> dict:
    return {
        "metrics": {"retrieval_coverage": 0.55, "gc_deletes": 3, "consolidations": 0},
        "recent_events": [
            {"ts": 1_800_000_000.0, "kind": "recall", "payload": {"query": "仓位"}},
            {"ts": 1_800_000_100.0, "kind": "miss", "payload": {"query": "止损"}},
        ],
        "sample_nodes": [
            {"tier": "core", "scope": "global", "content": "禁止绝对化措辞", "tags": ["硬规则"]},
            {"tier": "event", "scope": "proj:x", "content": "今天复盘发现追高了", "tags": ["复盘"]},
        ],
        "narrative": {"coherence": 0.62, "reliability": 0.8, "monopoly": 0.3},
    }


def test_parse_valid_json() -> None:
    reflector = LLMReflector(CallableProvider(lambda s, p: json.dumps(GOOD, ensure_ascii=False)))
    result = reflector.parse(json.dumps(GOOD, ensure_ascii=False))
    assert len(result.findings) == 2
    assert result.confidence == 0.72
    assert [p["param"] for p in result.suggested_params] == ["semantic_min_score"]
    assert set(result.rejected_params) == {"forget_w", "nonexistent"}
    assert not result.degraded


def test_parse_fenced_json() -> None:
    raw = "```json\n" + json.dumps(GOOD, ensure_ascii=False) + "\n```"
    result = LLMReflector(CallableProvider(lambda s, p: raw)).parse(raw)
    assert result.findings and not result.degraded


def test_parse_garbage_degrades() -> None:
    reflector = LLMReflector(CallableProvider(lambda s, p: "我觉得最近不太对劲。"))
    result = reflector.reflect(_payload())
    assert result.degraded and result.error
    assert result.findings == []


def test_empty_json_degrades() -> None:
    reflector = LLMReflector(CallableProvider(lambda s, p: '{"confidence": 0.9}'))
    result = reflector.reflect(_payload())
    assert result.degraded, "合法 JSON 但没有任何发现，等于没反思"


def test_provider_failure_degrades_instead_of_raising() -> None:
    from memory_palace.llm import LLMError

    def boom(system: str, prompt: str) -> str:
        raise LLMError("连接被拒绝")

    reflector = LLMReflector(CallableProvider(boom))
    result = reflector.reflect(_payload())
    assert result.degraded and "拒绝" in result.error
    lines = reflector.as_callable()(_payload())
    assert lines and "降级" in lines[0]


def test_prompt_respects_budget() -> None:
    reflector = LLMReflector(CallableProvider(lambda s, p: "{}"), max_chars=900, max_evidence_nodes=3)
    payload = _payload()
    payload["sample_nodes"] = [
        {"tier": "event", "scope": "g", "content": "很长的记忆 " * 200} for _ in range(20)
    ]
    prompt = reflector.build_prompt(payload)
    assert len(prompt) <= 900, f"证据没被截断：{len(prompt)} 字符"
    assert "retrieval_coverage" in prompt, "截断不能把指标砍掉"
    assert str(max(ALLOWED_PARAMS)) in prompt or "semantic_min_score" in prompt


def test_system_prompt_forbids_extra_params() -> None:
    assert "JSON" in SYSTEM_PROMPT
    assert "清单" in SYSTEM_PROMPT


def test_reflection_does_not_touch_config() -> None:
    """最重要的那条：模型只能提议，不能改。"""
    provider = CallableProvider(lambda s, p: json.dumps(GOOD, ensure_ascii=False))
    config = PalaceConfig(llm_provider="http", llm_endpoint="http://127.0.0.1:9/x")
    palace = MemoryPalace(config=config, reflector=LLMReflector(provider).as_callable())
    before = config.to_dict()
    palace.remember("a", "仓位按半凯利给", tags=("仓位",))
    palace.remember("b", "板块资金接力看连板基因", tags=("板块",))
    report = palace.reflect()
    after = palace._config.to_dict()
    assert before == after, "反思改动了配置"
    assert any("外部反思" in f for f in report.findings)
    assert palace.llm is None or palace.llm.last is None  # 显式传 reflector 时不建 llm 对象


def test_llm_reflector_wired_from_config() -> None:
    config = PalaceConfig(
        llm_provider="http",
        llm_endpoint="http://127.0.0.1:9/v1/chat/completions",
        llm_model="m",
    )
    reflector = make_reflector(config)
    assert reflector is not None
    palace = MemoryPalace(config=config)
    assert palace.llm is not None
    assert palace.reflection.reflector is not None


def test_no_llm_configured_means_no_hook() -> None:
    palace = MemoryPalace()
    assert palace.llm is None
    assert palace.reflection.reflector is None


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  [PASS] {name}")
    print("\nLLM 反思边界自测通过 ✅")
