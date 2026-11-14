"""test_openbox.py - 「开箱能不能用」与「反思能不能纠错」两块契约。

对应两条外部评审意见，都是真的，只是根因和修法不同：

1. 「默认体验是玩具级」：HashingEmbedder 读字面不读意思。零依赖是刻意的，
   所以修法不是把它换成依赖，而是**不把它当默认答案** ——
   `auto` 只在探到真模型时启用稠密通道，探不到就明说「现在走词法」。
   最坏的行为不是没有语义，而是**假装有语义**。这条必须用断言钉死。

2. 「LLM 反思是伪自由，越界建议被直接丢弃」：白名单本身是对的（模型不该
   改自己的人格），但「拒绝后不告诉模型」让它退化成一枚格式化器。
   修法是把拒绝原因回灌给它再问一轮，且把越界原话留档给人裁决。
"""
from __future__ import annotations

import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from memory_palace import embedding  # noqa: E402
from memory_palace.core import MutableClock, PalaceConfig  # noqa: E402
from memory_palace.llm import CallableProvider, LLMReflector  # noqa: E402
from memory_palace.palace import MemoryPalace  # noqa: E402
from memory_palace.selfcorrect import (  # noqa: E402
    ALLOWED_PARAMS,
    FORBIDDEN_PARAMS,
    PARAM_BOUNDS,
)

GOOD = {
    "findings": ["召回率掉了"],
    "hypotheses": ["门槛偏高"],
    "suggested_params": [
        {"param": "semantic_min_score", "direction": "down", "why": "误弃率上升"}
    ],
    "confidence": 0.7,
}

OUT_OF_SCOPE = {
    "findings": ["人格漂移在涨"],
    "hypotheses": ["该动学习率"],
    "suggested_params": [
        {"param": "persona_learning_rate", "direction": "down", "why": "漂移明显"}
    ],
    "confidence": 0.5,
}


def _payload() -> dict:
    return {
        "metrics": {"retrieval_coverage": 0.5, "gc_deletes": 3},
        "recent_events": [],
        "sample_nodes": [{"tier": "core", "scope": "global", "content": "不追高", "tags": ["硬规则"]}],
        "narrative": {"coherence": 0.6},
    }


# ---------------------------------------------------------------- auto 探测


@pytest.fixture(autouse=True)
def _clear_probe():
    embedding._AUTO_PROBE.clear()
    yield
    embedding._AUTO_PROBE.clear()


def _no_model(monkeypatch) -> None:
    monkeypatch.setattr(embedding, "_st_installed", lambda: False)
    monkeypatch.setattr(embedding, "_ollama_alive", lambda host, timeout=0.35: False)


def test_auto_does_not_silently_fall_back_to_hashing(monkeypatch):
    """探不到真模型时必须返回 None，而不是偷偷给一个 hashing 假装有语义。

    这是整块改动的核心断言。返回 hashing 会让 `channels['dense']` 变成 True，
    用户以为语义检索已经生效 —— 比明说「没有」糟得多。
    """
    _no_model(monkeypatch)
    cfg = PalaceConfig(embedding_provider="auto")
    assert embedding.make_embedder(cfg) is None
    verdict = embedding.probe_auto()
    assert verdict["chosen"] == ""
    assert "词法" in verdict["summary"]
    assert [c["alive"] for c in verdict["checked"]] == [False, False]


def test_auto_picks_local_ollama(monkeypatch):
    monkeypatch.setattr(embedding, "_st_installed", lambda: False)
    monkeypatch.setattr(embedding, "_ollama_alive", lambda host, timeout=0.35: True)
    emb = embedding.make_embedder(PalaceConfig(embedding_provider="auto"))
    assert isinstance(emb, embedding.OllamaEmbedder)
    assert embedding.probe_auto()["chosen"] == "ollama"


def test_auto_prefers_sentence_transformers(monkeypatch):
    monkeypatch.setattr(embedding, "_st_installed", lambda: True)
    monkeypatch.setattr(embedding, "_ollama_alive", lambda host, timeout=0.35: True)

    def boom(*a, **kw):
        raise embedding.EmbeddingError("模型没下过")

    monkeypatch.setattr(embedding, "SentenceTransformerEmbedder", boom)
    # 用户没点名模型 → 允许退回 ollama
    emb = embedding.make_embedder(PalaceConfig(embedding_provider="auto"))
    assert isinstance(emb, embedding.OllamaEmbedder)
    # 用户点名了模型 → 失败就是失败，不要偷偷换一个
    named = PalaceConfig(embedding_provider="auto", embedding_model="BAAI/bge-m3")
    assert embedding.make_embedder(named) is None


def test_explicit_empty_provider_stays_lexical():
    """显式 provider='' 不该触发任何探测（有人就是要零延迟的纯词法）。"""
    assert embedding.make_embedder(PalaceConfig(embedding_provider="")) is None


# ---------------------------------------------------------------- 白名单单一源


def test_param_whitelist_is_derived_from_bounds():
    """白名单和值域表必须是同一份。

    分开写的时候出现过「白名单里有、值域里没有」的洞：
    `PARAM_BOUNDS.get(p, (1e-4, 1.0))` 会用通用区间悄悄放行，
    看起来一切正常，实际上那道闸根本没关。
    """
    assert ALLOWED_PARAMS == frozenset(PARAM_BOUNDS)
    assert not (ALLOWED_PARAMS & FORBIDDEN_PARAMS)
    for name, bounds in PARAM_BOUNDS.items():
        low, high = bounds
        assert low < high, f"{name} 值域上界必须大于下界"


# ---------------------------------------------------------------- 两轮反思


def test_out_of_scope_proposal_gets_a_second_round(monkeypatch):
    script = [json.dumps(OUT_OF_SCOPE), json.dumps(GOOD)]
    provider = CallableProvider(lambda s, p: script.pop(0) if script else json.dumps(GOOD))
    reflector = LLMReflector(provider, max_rounds=2)
    result = reflector.reflect(_payload())
    assert provider.calls == 2, "越界后必须再问一轮，否则模型永远不知道被拒"
    assert result.repair_rounds == 1
    assert [p["param"] for p in result.suggested_params] == ["semantic_min_score"]
    assert result.rejected_params == ["persona_learning_rate"]
    # 越界原话要留档，不能只剩一个参数名
    assert any("persona_learning_rate" in item and "漂移明显" in item for item in result.insights)
    # 第二轮的 findings/hypotheses 覆盖第一轮（它看到了拒绝原因，结论更可靠）
    assert result.findings == GOOD["findings"]


def test_single_round_cost_when_disabled():
    """max_rounds=1 = 老行为：只调一次，成本不翻倍。"""
    provider = CallableProvider(lambda s, p: json.dumps(OUT_OF_SCOPE))
    reflector = LLMReflector(provider, max_rounds=1)
    result = reflector.reflect(_payload())
    assert provider.calls == 1
    assert result.repair_rounds == 0
    assert result.suggested_params == []
    assert result.rejected_params == ["persona_learning_rate"]


def test_second_round_failure_keeps_first_round():
    """第二轮挂了不能把第一轮一起判死 —— 有结论比「全绿但没有」好。"""
    calls = {"n": 0}

    def fn(system: str, prompt: str) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            return json.dumps(OUT_OF_SCOPE)
        raise RuntimeError("模型超时")

    provider = CallableProvider(fn)
    result = LLMReflector(provider, max_rounds=2).reflect(_payload())
    assert result.findings == OUT_OF_SCOPE["findings"]
    assert result.degraded is False
    assert result.repair_rounds == 0


def test_clean_first_round_never_pays_twice():
    provider = CallableProvider(lambda s, p: json.dumps(GOOD))
    result = LLMReflector(provider, max_rounds=2).reflect(_payload())
    assert provider.calls == 1
    assert result.repair_rounds == 0


# ---------------------------------------------------------------- 落盘强度


def test_unknown_synchronous_rejected():
    with pytest.raises(ValueError, match="store_synchronous"):
        PalaceConfig(store_synchronous="OFF").validate()


def test_synchronous_reaches_sqlite(tmp_path):
    """配置项必须真的到 PRAGMA 上，不能只是 dataclass 里的一个字段。"""
    for mode, expected in (("NORMAL", 1), ("FULL", 2)):
        path = str(tmp_path / f"{mode}.sqlite")
        cfg = PalaceConfig(store_synchronous=mode, embedding_provider="")
        palace = MemoryPalace(path=path, clock=MutableClock(), autosave=True, config=cfg)
        palace.remember("x", "一句测试记忆", tags=("测试",))
        assert palace._store.conn.execute("PRAGMA synchronous").fetchone()[0] == expected
        assert palace._store.conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        palace.close()
