"""memory_palace - 会遗忘、会被情绪加固、会讲自己故事的记忆系统。

    from memory_palace import MemoryPalace, PalaceConfig, MutableClock

引擎与它们的边界：
    affect        情感权重（潮水 / 刻痕 / 失真代价）+ 评价器（情绪从文本与结果来，带依据）
    logic         逻辑关联（有向、有类型、可传递、可矛盾）
    consolidation 沉淀（碎片 → 高层记忆）
    value         选择（重要性 / 熵 / 结构熵 / 代谢预算 / 可审计的遗忘）
    narrative     自我叙事（投影而非节点；叙事地位会随时间退潮）
    persona       人格分层（基座只读 / 人格生长 / 人设可换 / 变更留痕）
    style         风格固化与对立裁决
    reflection    自我回看（只提建议，不自动执行）
    selfcorrect   自修正（12 个白名单参数 + 值域 + 抗滞后验证 + 可回滚）
    semantics     语义检索（词法通道 + 稠密通道 + 混合打分）
    embedding     嵌入器（内置哈希 / 任意 OpenAI 兼容端点 / 磁盘缓存）
    store         持久化门面（SQLite WAL / JSON）+ 导出导入
    journal       追加日志（CRC32 + 前滚），JSON 后端的崩溃恢复
    locking       跨进程字节锁
    evaluation    组件级评测（检索 / 弃答 / 去重 / 沉淀）与阈值标定
    llm           LLM 反思（结构化输出、白名单、降级）
"""
from .affect import (
    OUTCOMES,
    Affect,
    AffectEngine,
    Appraisal,
    appraise_text,
)
from .consolidation import ConsolidationEngine, ConsolidationResult, default_summary
from .core import (
    DAY,
    WRITE_STATS,
    Edge,
    Event,
    MemoryGraph,
    MemoryNode,
    MutableClock,
    PalaceConfig,
    atomic_write_json,
    read_json,
)
from .logic import LogicEdge, LogicGraph, Relation
from .memory_model import KINDS, KIND_ORDER, MemoryKind, MemoryModel, classify
from .narrative import Chapter, NarrativeEngine, NarrativeRevision
from .palace import STATE_VERSION, SUPPORTED_STATE_VERSIONS, MemoryPalace
from .persona import (
    TRAIT_KEYS,
    Amendment,
    Foundation,
    Persona,
    PersonaEngine,
    PersonaRevision,
    PersonalityEngine,
    Trait,
    TraitSpec,
    default_foundation,
    drift_report,
)
from .reflection import ReflectionEngine, ReflectionReport
from .selfcorrect import (
    ALLOWED_PARAMS,
    FORBIDDEN_PARAMS,
    PARAM_BOUNDS,
    Candidate,
    Escalation,
    Patch,
    SelfCorrector,
)
from .semantics import ConceptEncoder, SemanticIndex, SparseVector, cosine
from .embedding import (
    DenseIndex,
    Embedder,
    EmbeddingError,
    HashingEmbedder,
    HttpEmbedder,
    make_embedder,
)
from .store import (
    JsonStore,
    LoadResult,
    SaveResult,
    Store,
    StoreCorrupt,
    StoreEmpty,
    StoreError,
    open_store,
    remove_store_files,
)
from .journal import Journal
from .locking import FileLock, LockBusy
from .llm import CallableProvider, HttpChatProvider, LLMReflector, LLMError, make_reflector
from .style import Adjudication, StyleEngine, StyleRule
from .value import ValueModel

__version__ = "0.10.0"

__all__ = [
    "__version__",
    "Store",
    "JsonStore",
    "open_store",
    "remove_store_files",
    "SaveResult",
    "LoadResult",
    "StoreError",
    "StoreCorrupt",
    "StoreEmpty",
    "Journal",
    "FileLock",
    "LockBusy",
    "Embedder",
    "HashingEmbedder",
    "HttpEmbedder",
    "DenseIndex",
    "EmbeddingError",
    "make_embedder",
    "LLMReflector",
    "LLMError",
    "HttpChatProvider",
    "CallableProvider",
    "make_reflector",
    "MemoryPalace",
    "PalaceConfig",
    "MemoryNode",
    "MemoryGraph",
    "Edge",
    "Event",
    "MutableClock",
    "Affect",
    "AffectEngine",
    "Appraisal",
    "appraise_text",
    "OUTCOMES",
    "LogicGraph",
    "LogicEdge",
    "Relation",
    "ValueModel",
    "NarrativeEngine",
    "NarrativeRevision",
    "Chapter",
    "ConsolidationEngine",
    "ConsolidationResult",
    "default_summary",
    "StyleEngine",
    "StyleRule",
    "Adjudication",
    "SemanticIndex",
    "ConceptEncoder",
    "cosine",
    "SparseVector",
    "Foundation",
    "TraitSpec",
    "Trait",
    "PersonalityEngine",
    "Persona",
    "PersonaEngine",
    "PersonaRevision",
    "Amendment",
    "TRAIT_KEYS",
    "default_foundation",
    "drift_report",
    "MemoryModel",
    "MemoryKind",
    "KINDS",
    "KIND_ORDER",
    "classify",
    "SelfCorrector",
    "Patch",
    "Candidate",
    "Escalation",
    "ALLOWED_PARAMS",
    "FORBIDDEN_PARAMS",
    "PARAM_BOUNDS",
    "ReflectionEngine",
    "ReflectionReport",
    "atomic_write_json",
    "read_json",
    "STATE_VERSION",
    "SUPPORTED_STATE_VERSIONS",
    "WRITE_STATS",
    "DAY",
]
