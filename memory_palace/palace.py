"""palace.py - 顶层编排：把情感、沉淀、遗忘、叙事、风格、反思串成一个可运行的生命周期。

每轮固定顺序：
  风格结算 → 沉淀 → 拼命抓住（预算）→ 遗忘 → 自述 → 人格结算 → 反思 → 自修正

次序都是刻意排的：沉淀必须早于 GC，否则刚要结晶的碎片会先被扫走；
「抓住」必须早于 GC，否则想留的留不下；自述必须晚于 GC，否则会讲到已经不在的东西；
人格结算排在自述之后，因为它要读「这一轮讲了什么故事」来判断自己有没有变；
反思必须最后，才看得见本轮全部事件；自修正读的就是这份反思，且只能在它之后 ——
先验旧账再结新账，否则新修正会叠在未验证的假设上。

写入侧还有一件容易被忽略的事：情绪不是调用方拍出来的数字。
remember() 不传 valence / arousal 时走评价器（文本依据），运行时用 feel_outcome()
从结果反推 —— 两个数永远带着来源，可以追问「它凭什么发烫」。
"""
from __future__ import annotations

import functools
import os
import random
import threading
import time
from collections import Counter
from contextlib import contextmanager
from typing import Any, Callable, Iterator

from .affect import AffectEngine
from .consolidation import ConsolidationEngine, ConsolidationResult, Summarizer
from .core import (
    DAY,
    WRITE_STATS,
    Clock,
    Event,
    MemoryGraph,
    MemoryNode,
    PalaceConfig,
    dataclass_field_names,
    read_json,
    wall_clock,
)
from .logic import LogicGraph, Relation
from .memory_model import MemoryModel, classify, kind_spec
from .narrative import NarrativeEngine, NarrativeRevision
from .persona import (
    Amendment,
    Foundation,
    Persona,
    PersonaEngine,
    PersonaRevision,
    PersonalityEngine,
    TraitSpec,
    default_foundation,
    drift_report,
)
from .reflection import ReflectionEngine, ReflectionReport
from .selfcorrect import SelfCorrector
from .pragmatics import Speech, classify as classify_speech
from .scope import (
    GLOBAL,
    detect_project,
    normalize_project,
    normalize_scope,
)
from .locking import LockBusy
from .llm import make_reflector
from .store import Store, StoreEmpty, StoreError, open_store
from .semantics import Encoder, SemanticIndex, cosine
from .style import StyleEngine
from .value import ValueModel

# v7 起快照带 revision / state_id / checksum，并允许 journal 前滚。
# v4~v6 快照仍可直接读（缺的字段走默认值），写回时自动升到 v7。
STATE_VERSION = 7
SUPPORTED_STATE_VERSIONS = (4, 5, 6, 7)

# amend_foundation 的哨兵：traits={"__remove__": ["playfulness"]} 表示删除维度
_REMOVE = "__remove__"


def _mutating(reason: str = ""):
    """给写操作套一层「跨进程事务」。

    为什么放在装饰器而不是每个方法里手写：写路径有二十来个入口，
    漏掉一个就等于给「丢更新」留了一个后门，而这种后门在单进程测试里永远不出现。
    装饰器让「哪些方法是写操作」变成可以 grep 出来的事实。
    """

    def wrap(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        def inner(self: "MemoryPalace", *args: Any, **kwargs: Any) -> Any:
            def attempt() -> Any:
                with self._txn(reason or fn.__name__):
                    return fn(self, *args, **kwargs)

            return self._retry_locked(attempt, reason or fn.__name__)

        return inner

    return wrap


class MemoryPalace:
    def __init__(
        self,
        path: str | None = None,
        clock: Clock | None = None,
        config: PalaceConfig | None = None,
        summarizer: Summarizer | None = None,
        encoder: Encoder | None = None,
        autosave: bool = False,
        project: str | None = None,
        reflector: Callable[[dict[str, Any]], list[str]] | None = None,
        store: DurableStore | None = None,
        concurrency: str | None = None,
        embedder: Any = None,
        backend: str | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._clock: Clock = clock or wall_clock
        self._config = config or PalaceConfig()
        # 显式传参优先于配置：同一个库既可能被 CLI（单进程）用，也可能被 server（多进程）用。
        if concurrency:
            self._config.concurrency = concurrency
        if backend:
            self._config.backend = backend
        # 边界必须校验：`PalaceConfig(...)` 本身不会自动验证，而一个坏参数不会报错，
        # 只会让系统「安静地做错事」—— semantic_alpha=-5 会让检索永远返回空，
        # forget_w=-1 会让所有记忆立刻可忘，grasp_budget=nan 会让预算判断全假。
        # 到这里为止还没读过任何状态，是唯一能干净失败的位置。
        self._config.validate()
        self._config_override = config
        self._summarizer = summarizer
        self._autosave = autosave
        self._path = path
        # 当前项目：决定「默认写进哪里」与「默认读哪些」。显式传优先，否则按 cwd 推断。
        self.project = normalize_project(project) if project else detect_project()

        # —— 持久化后端：默认 SQLite（WAL）；没有 path 就只有内存 ——
        self._store: Store | None = store
        if self._store is None and path:
            self._store = open_store(
                path,
                backend=self._config.backend,
                lock_timeout=self._config.lock_timeout,
                journal_enabled=self._config.journal_enabled,
                journal_keep=self._config.journal_keep,
                verify=self._config.verify_checksum,
                snapshot_every=self._config.snapshot_every,
                validate=lambda s: self._validate_state(s, self._path or ""),
            )
        if self._store is not None:
            self._path = self._store.path
            if hasattr(self._store, "event_limit"):
                self._store.event_limit = self._config.max_events  # type: ignore[attr-defined]
        self.revision = 0
        self.state_id = ""
        self.recovered = False
        self.conflict_count = 0
        self.last_save_result: dict[str, Any] | None = None
        self._txn_depth = 0
        self._stale_after_rollback = False
        # 增量写需要的两个游标：哪些名字已经在库里、事件写到了第几条。
        # 它们只影响「要不要写」，不影响「写什么」—— 写什么永远由内存里的真实状态决定。
        self._persisted_nodes: set[str] = set()
        self._event_cursor = 0

        self.graph = MemoryGraph()
        self.style = StyleEngine(self._config)
        self.semantics = SemanticIndex(
            encoder, config=self._config, embedder=embedder
        )
        self.consolidation = ConsolidationEngine(
            self._config, summarizer, vector_lookup=self._vector_of
        )
        # 外部反思钩子：传了就用（显式传参优先，其次按配置造 LLM 反思器），
        # 都没有就是规则化监控。它只产出文字，不改配置 ——
        # 让模型直接改参数等于让它改自己的人格。
        self.llm: Any = None
        if reflector is None:
            self.llm = make_reflector(self._config)
            reflector = self.llm.as_callable() if self.llm is not None else None
        self.reflection = ReflectionEngine(self._config, reflector=reflector)
        self.affect = AffectEngine(self._config)
        self.logic = LogicGraph()
        self.value = ValueModel(self._config)
        self.narrative = NarrativeEngine(self._config)
        # 人格三层：基座（只读）→ 人格（生长）→ 人设（可选可换）
        self.foundation = default_foundation(0.0)
        self.personality = PersonalityEngine(self._config, self.foundation)
        self.persona = PersonaEngine(self._config)
        self.memory = MemoryModel(self._config)
        self.corrector = SelfCorrector(self._config)
        self.events: list[Event] = []
        self.tombstone: dict[str, MemoryNode] = {}
        self.last_report: dict[str, Any] | None = None
        self.last_gc: list[dict[str, Any]] = []
        # 最近一次落盘是否成功。写到磁盘之前，任何「已保存」的说法都不成立，
        # 调用方（尤其是自动化任务）需要一个能读到的位，而不是去翻事件流。
        self.last_save_ok: bool = True
        # None = 本轮根本没抓过（不能据此判「预算不足」）；[] = 抓过但一条没抓到。
        self._last_grasp: list[dict[str, Any]] | None = None
        # 评估缓存：一次 context 里 assess 会被 entropy / snapshot / 排序各要一遍，
        # 不缓存就是同一份 O(n log n) 算三四次。键里带「1 秒桶」，代价 <1 秒的衰减误差。
        self._assess_cache: tuple[tuple, dict[str, dict[str, float]]] | None = None
        self._logic_rev = 0
        self._last_persona_health: tuple[str, bool, bool] | None = None

        if path and os.path.exists(path):
            self._load_unlocked(path, config_override=config)
        elif self._store is not None and self._store.exists():
            self._load_unlocked(self._store.path, config_override=config)

    def now(self) -> float:
        return self._clock()

    def _vector_of(self, node: MemoryNode) -> Any:
        """沉淀聚类取向量；未建索引的节点按需补建，缺了就诚实返回 None。

        返回稀疏向量：聚类要对 N² 对做余弦，稠密数组每对要扫 263 维，
        而一条记忆平均只有 18 个非零分量 —— 这里也是量级差别。
        """
        # 先对齐：向量必须与库内同一把 IDF 尺算出来的才能比。
        # 索引里可能还留着「写入时用旧语料编码」的向量，用它做聚类等于拿两把尺量。
        self.semantics.sync(self.graph)
        vec = self.semantics.sparse(node.name)
        if vec is None:
            try:
                self.semantics.upsert(node)
            except ValueError:
                return None
            vec = self.semantics.sparse(node.name)
        return vec

    # ---------- 内部工具 ----------

    def _log(self, kind: str, payload: dict[str, Any] | None = None, ts: float | None = None) -> None:
        self.events.append(Event(ts if ts is not None else self.now(), kind, payload or {}))
        self._trim_events()

    def _trim_events(self) -> None:
        limit = self._config.max_events
        if len(self.events) > limit:
            self.events = self.events[-limit:]

    # ---------- 并发写：跨进程事务 ----------

    @contextmanager
    def _txn(self, reason: str = "write") -> Iterator[None]:
        """写事务：开事务（SQLite: BEGIN IMMEDIATE / JSON: 文件锁）→ 若磁盘更新先重载
        → 执行修改 → 提交。

        「先重载再修改」是这里唯一重要的事。反过来（先改后查）两边都认为自己最新，
        后写的一方会把前一方**已经提交**的修改整段覆盖，而没有任何一方报错 ——
        这是丢数据最隐蔽的形态，也是「多进程写同一份记忆」以前不适用的根本原因。

        `BEGIN IMMEDIATE` 而不是 `BEGIN`：后者要等到第一次写才升级锁，
        两个进程可能都读完了才发现要抢同一个写锁，白白多一次死锁/重试。
        一开始就声明「我是写者」，排队的人从第一毫秒就在排队。

        重入是允许的（`_txn_depth`）：外层已经开过事务并重载过，内层不必再来一遍，
        更不能在内层重载 —— 那会把外层尚未提交的修改直接扔掉。
        """
        with self._lock:
            outer = self._txn_depth == 0
            self._txn_depth += 1
            begun = False
            try:
                if outer and self._store is not None and self._path and (
                    self._config.concurrency == "process"
                ):
                    self._store.begin(reason)
                    begun = True
                    stale = self._stale_after_rollback
                    self._stale_after_rollback = False
                    self._refresh_from_disk(force=stale)
                yield
            except BaseException:
                if begun and self._store is not None:
                    self._store.rollback()
                    # 事务回滚了，但内存里的修改还在 —— 下一次事务必须先对齐磁盘，
                    # 否则这份「从未提交过」的修改会混进下一次提交。
                    self._stale_after_rollback = True
                raise
            else:
                if begun and self._store is not None:
                    self._store.commit()
            finally:
                self._txn_depth -= 1

    def _retry_locked(self, call: Callable[[], Any], label: str, attempts: int = 3) -> Any:
        """写锁等不到时重试几次，而不是把用户的写入直接判死。

        这个重试是 CI 逼出来的：4 个进程在 2 核 runner 上并发写时，
        持锁的那个会被操作系统调度走，其他人干等到 10s 超时 —— 写入失败，
        但数据本身完全正常，只是「排队排久了」。对一个记忆系统来说，
        「这次没记住」比「晚 200ms 记住」严重得多。

        只在事务**未能开启**时重试（`_txn_depth == 0`）：一旦进入事务再失败，
        重试会把已经执行了一半的修改再做一遍。
        """
        last: BaseException | None = None
        for index in range(max(1, attempts)):
            try:
                return call()
            except LockBusy as exc:
                if self._txn_depth > 0 or index == attempts - 1:
                    raise
                last = exc
                time.sleep(0.05 * (2**index) + random.uniform(0.0, 0.05))
        raise last if last else LockBusy(label, 0.0, None)  # pragma: no cover

    def refresh(self, force: bool = False) -> bool:
        """把别人的提交读进来（只读路径也调用）。

        以前只有写事务会重载，于是长驻进程（MCP server / HTTP 服务）看到的永远是
        「自己上次写完那一刻」的库 —— 另一个进程写进去的记忆，它一条也看不见，
        而且不会报错。这在单进程里测不出来，在多进程里是致命的。

        代价是一次 `peek_revision()`（SQLite 一次主键查询，约 0.05ms）。
        事务进行中不重载：那会把尚未提交的修改扔掉。
        """
        with self._lock:
            if self._txn_depth:
                return False
            if self._store is None or not self._path:
                return False
            return self._refresh_from_disk(force=force)

    def _refresh_from_disk(self, force: bool = False) -> bool:
        """磁盘比内存新时，先把别人的提交读进来。返回是否发生了重载。

        只在写事务开头调用，且此时还没有任何本事务的修改 ——
        所以整库重载是安全的，不会吞掉自己刚做的事。
        """
        if self._store is None or not self._path:
            return False
        if not force:
            disk = self._store.peek_revision()
            if disk is None or disk == self.revision:
                return False
        try:
            result = self._store.load(
                validate=lambda s: self._validate_state(s, self._path or "")
            )
        except StoreEmpty:
            return False
        self._restore_state(result.payload, self._path, self._config_override)
        self.revision = result.revision
        self.state_id = result.state_id
        self.conflict_count += 1
        self._log("concurrent_reload", {"revision": result.revision}, self.now())
        return True

    def _maybe_save(self) -> bool:
        """自动保存失败不得炸穿主流程，但必须留下可观测的痕迹。"""
        if not (self._autosave and self._path):
            return True
        try:
            durable = self._save_unlocked(self._path, reason="autosave")
            self.last_save_ok = bool(durable)
            return bool(durable)
        except (OSError, StoreError, LockBusy) as exc:
            self.last_save_ok = False
            self._log("save_failed", {"path": self._path, "error": str(exc)})
            return False

    # ---------- 写入与检索 ----------

    @_mutating("remember")
    def remember(
        self,
        name: str,
        content: str,
        tags: tuple[str, ...] = (),
        tier: str = "event",
        pinned: bool = False,
        origin: str = "external",
        valence: float | None = None,
        arousal: float | None = None,
        kind: str = "",
        supersedes: tuple[str, ...] | list[str] | str = (),
        merge: bool = False,
        scope: str = "",
        speech: "Speech | None" = None,
        auto_tier: bool = False,
    ) -> MemoryNode:
        """写入一条记忆。情绪是**写入时**的属性，不是事后贴的标签。

        两个和「时间」有关的参数：

        `supersedes` 命中旧记忆时，旧记忆**不被删除，而是到期**（valid_to = now），
        并记下 `superseded_by`。这样「现在是什么」与「当时是什么」都能回答 ——
        直接删掉会把「我确实这么想过」也一起抹了，不删又会两句话一起端给模型。

        `merge=True` 时，与现有记忆几乎一字不差（≥ dedupe_similarity）就不新开节点，
        而是强化旧节点并把标签并进去。默认关闭：库层要可预测，CLI/agent 入口才默认开。

        不传 valence / arousal 时，由评价器从内容与标签推断 —— 这是「情绪从哪来」
        的答案：不是凭空一个 0.9，而是「因为文本里出现了『重挫』和『极其』」。
        显式传值永远优先，且不会被评价器覆盖。

        kind 决定它属于哪种记忆（工作/情景/语义/程序/身份），
        因而决定它的寿命量级与能否被遗忘。不填就按属性推断。
        """
        with self._lock:
            now = self.now()
            # 语用分类：这句话是禁令、请求、暗示还是撤回？
            # 只标注、默认不改层级 —— 自动改 tier 会在误判时把一条随口的话变成规矩。
            # 需要自动分层时显式传 auto_tier=True（CLI/MCP 的 agent 入口会开）。
            speech = speech or classify_speech(content)
            if auto_tier:
                tier = speech.suggested_tier()

            if merge:
                twin = self._find_duplicate(content, now)
                if twin is not None:
                    before = twin.access_count
                    twin.access(now, self._config.access_gain)
                    if tags:
                        twin.tags = tuple(dict.fromkeys(twin.tags + tuple(tags)))
                    if twin.speech.force < speech.force:
                        # 同一句话先当陈述、后当禁令时，以更硬的那次为准
                        twin.speech = speech
                    self._log(
                        "merge",
                        {
                            "into": twin.name,
                            "skipped": name,
                            "access_before": before,
                            "access_after": twin.access_count,
                        },
                        now,
                    )
                    self._maybe_save()
                    return twin
            node = MemoryNode(
                name=name,
                content=content,
                tier=tier,
                tags=tuple(tags),
                created_at=now,
                last_accessed=now,
                pinned=pinned,
                origin=origin,
                affect=self.affect.blank(now),
                kind=kind,
                scope=normalize_scope(scope, self.project),
                speech=speech,
            )
            if valence is not None or arousal is not None:
                v = 0.0 if valence is None else float(valence)
                a = 0.0 if arousal is None else float(arousal)
                self.affect.feel(node.affect, now, v, a)
                node.affect.source = "explicit" if (v or a) else "neutral"
            else:
                appraisal = self.affect.appraise(content)
                self.affect.feel_appraisal(node.affect, now, appraisal)
                if appraisal.drivers:
                    self._log(
                        "appraise",
                        {"name": name, **appraisal.to_dict()},
                        now,
                    )
            # 旧的不是消失，是到期。必须在 validate 之前写，才能被一起校验。
            retired: list[str] = []
            requested = [supersedes] if isinstance(supersedes, str) else list(supersedes or ())
            for old_name in dict.fromkeys(requested):
                old = self.graph.get(old_name)
                if old is None or old_name == name or old.is_retired():
                    continue
                old.valid_to = now
                old.superseded_by = name
                retired.append(old_name)
                self._log(
                    "supersede",
                    {"old": old_name, "by": name, "valid_to": now},
                    now,
                )
            node.supersedes = tuple(retired)

            self.memory.enroll(node)
            node.validate()  # 类型名拼错必须当场炸
            stored = self.graph.add_node(node)
            if stored is not node:
                # 同名记忆已存在。以前这里会静默返回旧节点、把新内容丢掉 ——
                # 「写进去了」与「什么都没发生」在返回值上完全一样，最坏的那种失败。
                # 正确的语义是：同名 = 同一条记忆，就地更新它。
                stored.content = content
                if tags:
                    stored.tags = tuple(tags)
                stored.tier = tier
                stored.pinned = bool(pinned) or stored.pinned
                stored.kind = node.kind
                stored.speech = speech
                stored.access(now, self._config.access_gain)
                self.semantics.upsert(stored)
                self._log("replace", {"name": name, "reason": "same_name"}, now)
                self._maybe_save()
                return stored
            self.semantics.upsert(node)
            self._maybe_save()
            return node

    def _find_duplicate(self, content: str, now: float) -> MemoryNode | None:
        """找一条「几乎就是同一句话」的现有记忆。

        用字符 bigram 余弦而不是语义分：去重要的是**字面几乎相同**，
        语义相近恰恰是不该合并的情况（补充了新信息）。
        """
        text = (content or "").strip()
        if not text:
            return None
        # 先对齐：库内向量是按「当前语料的 IDF」算的，查询侧必须用同一把尺，
        # 否则余弦是在两个不同空间之间做比较，结果无意义。
        self.semantics.sync(self.graph)
        try:
            fresh = self.semantics.encode_query(text)
        except ValueError:
            return None
        best_sim, best_node = -1.0, None
        for node in self.graph.all_nodes():
            if node.is_retired() or not node.is_valid_at(now):
                continue
            # 长度比是硬门槛：包含新内容的更长句子，余弦仍然很高，但绝不能合并。
            span = max(len(text), len(node.content))
            if span and min(len(text), len(node.content)) / span < self._config.dedupe_length_ratio:
                continue
            vec = self.semantics.sparse(node.name)
            if vec is None:
                continue
            sim = cosine(fresh, vec)
            if sim > best_sim:
                best_sim, best_node = sim, node
        if best_node is None or best_sim < self._config.dedupe_similarity:
            return None
        return best_node

    # ---------- 情感 ----------

    @_mutating("feel")
    def feel(self, name: str, valence: float, arousal: float) -> MemoryNode | None:
        """给一条已存在的记忆追加情绪。注意：这**不会**强化它，只改它的衰减速度。"""
        with self._lock:
            node = self.graph.get(name)
            if node is None:
                return None
            now = self.now()
            self.affect.feel(node.affect, now, valence, arousal)
            self._log(
                "feel",
                {"name": name, "valence": node.affect.valence, "arousal": node.affect.arousal},
                now,
            )
            self._maybe_save()
            return node

    @_mutating("feel_outcome")
    def feel_outcome(self, name: str, outcome: str) -> MemoryNode | None:
        """运行时结果反馈：止损兑现 / 被反证 / 顺利想起 —— 情绪由此而来。

        与 `feel` 一样，只改情绪档案，**不强化记忆**（不碰 base_weight / last_access）。
        """
        with self._lock:
            node = self.graph.get(name)
            if node is None:
                return None
            now = self.now()
            self.affect.feel_outcome(node.affect, now, outcome)
            self._log(
                "feel_outcome",
                {
                    "name": name,
                    "outcome": outcome,
                    "valence": node.affect.valence,
                    "arousal": node.affect.arousal,
                    "drivers": list(node.affect.drivers),
                },
                now,
            )
            self._maybe_save()
            return node

    @_mutating("reappraise")
    def reappraise(self, name: str, valence: float) -> MemoryNode | None:
        """重估：改的是「现在怎么看」，不是「当时发生了什么」。内容与强度都不动。"""
        with self._lock:
            node = self.graph.get(name)
            if node is None:
                return None
            now = self.now()
            before = node.affect.valence
            self.affect.reappraise(node.affect, now, valence)
            self._log(
                "reappraise",
                {"name": name, "from": before, "to": valence, "content_intact": True},
                now,
            )
            self._maybe_save()
            return node

    # ---------- 逻辑关联 ----------

    def _bump_logic(self) -> None:
        self._logic_rev += 1

    @_mutating("assert_relation")
    def assert_relation(
        self,
        src: str,
        dst: str,
        kind: str | Relation,
        strength: float = 1.0,
    ) -> None:
        """声明一条逻辑关系。两端必须真实存在 —— 悬空断言一律拒绝。"""
        with self._lock:
            for name in (src, dst):
                if name not in self.graph:
                    raise KeyError(f"关系端点不存在: {name}")
            now = self.now()
            edge = self.logic.assert_relation(src, dst, kind, strength, now)
            self._bump_logic()
            self._log(
                "assert_relation",
                {
                    "src": edge.src,
                    "dst": edge.dst,
                    "kind": edge.kind,
                    "strength": round(edge.strength, 6),
                },
                now,
            )
            self._maybe_save()

    def support_of(self, name: str) -> float:
        """这条记忆有多少凭据。孤立碎片 = 0，孤证不立不是修辞。"""
        with self._lock:
            return self.logic.support(name)

    def consequences(self, name: str, max_hops: int | None = None) -> dict[str, float]:
        """从它出发能推出什么、强度多少。"""
        with self._lock:
            hops = self._config.logic_max_hops if max_hops is None else max_hops
            return self.logic.closure(name, max_hops=hops, hop_decay=self._config.logic_hop_decay)

    # ---------- 自我叙事 ----------

    @_mutating("tell")
    def tell(self) -> NarrativeRevision:
        """让系统把此刻的自己讲一遍，并如实交出它放下了什么。"""
        with self._lock:
            revision = self._narrate_unlocked(self.now())
            self._maybe_save()
            return revision

    def narrative_head(self) -> dict[str, Any] | None:
        with self._lock:
            head = self.narrative.head
            return head.to_dict() if head else None

    def _state_stamp(self, now: float) -> tuple:
        """缓存键：节点规模 + 访问计数和 + 最后访问时刻和 + 逻辑版本 + 秒级时间桶。

        不用「内容哈希」：那要扫全部文本；这四个整数足以捕捉「权重/结构变了」。
        """
        nodes = self.graph.all_nodes()
        return (
            len(nodes),
            sum(n.access_count for n in nodes),
            int(sum(n.last_accessed for n in nodes)),
            self._logic_rev,
            int(now),
        )

    def assessment(self, now: float | None = None) -> dict[str, dict[str, float]]:
        """一次计算、多处复用的重要性账本（context / 树 / 熵 / 快照共用）。"""
        with self._lock:
            moment = self.now() if now is None else now
            stamp = self._state_stamp(moment)
            if self._assess_cache is not None and self._assess_cache[0] == stamp:
                return self._assess_cache[1]
            result = self.value.assess(self.graph, moment, self.logic.support_map())
            self._assess_cache = (stamp, result)
            return result

    def structural(self, now: float | None = None) -> dict[str, float]:
        with self._lock:
            return self.value.structural(self.graph, self.now() if now is None else now)

    def importance_of(self, name: str) -> dict[str, float] | None:
        """一条记忆为什么值得留 / 该被放手：四个分量逐一列出。"""
        with self._lock:
            node = self.graph.get(name)
            if node is None:
                return None
            return self.value.importance(node, self.now(), self.logic.support(name))

    def entropy(self, scopes: set[str] | None = None) -> dict[str, Any]:
        """「我在乎的东西」的分布 + 图本身的形状：全平=什么都无所谓，过陡=只押一件事，
        全是孤岛=记下了却彼此无关。三个读数分开报，不合成一个自我安慰的总分。

        `scopes` 让读数与「当前视图」一致：看着项目视图却报全库的熵，
        会让人误判「我的注意力很集中」，其实是被别的项目的数据摊平了。
        """
        with self._lock:
            now = self.now()
            assessment = self.assessment(now)
            if scopes is not None:
                assessment = {
                    k: v for k, v in assessment.items()
                    if (node := self.graph.get(k)) is not None and node.scope in scopes
                }
            dist = self.value.distribution(assessment)
            struct = self.structural(now)
            return {
                **dist,
                "structure": struct,
                "health": self.value.health(dist),
                "structure_health": self.value.structure_health(struct),
            }

    def semantic_explain(self, text: str) -> dict[str, Any]:
        """这条查询为什么命中这些概念？多义词被分给了谁？哪些词是从语料学来的？"""
        with self._lock:
            self.semantics.sync(self.graph)
            return self.semantics.explain(text)

    @_mutating("age")
    def age(self, name: str, days: float) -> MemoryNode | None:
        """把某条记忆的最后访问时间往回拨（管理/测试用）。"""
        with self._lock:
            node = self.graph.get(name)
            if node is not None:
                node.last_accessed -= days * DAY
            return node

    @_mutating("pin")
    def pin(self, name: str, pinned: bool = True) -> MemoryNode | None:
        """钉死 / 解钉。钉死 = 不衰减、不参与自动遗忘，只能在解钉后才会被放手。

        这是用户对系统唯一的「主权开关」：自动遗忘可以很聪明，但「这条不许动」
        必须由人说了算。钉死时顺手把强度抬到初始值，否则解钉瞬间它可能已经烂掉。
        """
        with self._lock:
            node = self.graph.get(name)
            if node is None:
                return None
            node.pinned = pinned
            if pinned:
                node.base_weight = max(node.base_weight, 1.0)
            self._log("pin" if pinned else "unpin", {"name": name}, self.now())
            self._maybe_save()
            return node

    @_mutating("promote")
    def promote(self, name: str, to: str = GLOBAL) -> MemoryNode | None:
        """把一条记忆提升为跨项目（或改成指定作用域）。

        存在的理由：项目里学到的教训，常常是「原来这类问题都该这么办」。
        让它留在项目里，下个项目会重新踩一遍；直接写全局，又会把项目细节带到别处。
        所以提升是**一次显式决定**，不是自动泛化 —— 自动泛化必然把特例当通例。
        """
        with self._lock:
            node = self.graph.get(name)
            if node is None:
                return None
            before = node.scope
            node.scope = normalize_project(to) if to != GLOBAL else GLOBAL
            self._log(
                "promote",
                {"name": name, "from": before, "to": node.scope},
                self.now(),
            )
            self._maybe_save()
            return node

    def scope_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for node in self.graph.all_nodes():
            counts[node.scope] = counts.get(node.scope, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))

    @_mutating("drop")
    def drop(self, name: str, reason: str = "user_requested") -> bool:
        """显式丢弃一条记忆（人要求的删除，不是系统判的遗忘）。

        走与 GC 相同的清理路径：进墓碑（可 restore）→ 移出图 → 移出语义索引 →
        剪掉指向它的逻辑断言。少任何一步都会留下幽灵：悬空的推理边或检索命中已删记忆。
        """
        with self._lock:
            node = self.graph.get(name)
            if node is None:
                return False
            self.tombstone[name] = node
            self.graph.remove_nodes_from([name])
            self.semantics.forget(name)
            self.logic.prune([name])
            self._bump_logic()
            self._log("drop", {"name": name, "reason": reason}, self.now())
            self._maybe_save()
            return True

    def _score_components(
        self,
        query: str,
        query_tags: tuple[str, ...],
        now: float,
        scopes: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        """三路打分的拆解：语义 / 标签 / 时近。公开给 score_breakdown，保证可解释。"""
        cfg = self._config
        wanted = set(query_tags)
        text = (query or "").strip()
        if text:
            self.semantics.sync(self.graph)  # 语料变了先重学词典，再检索
        sem_scores = self.semantics.scores(text) if text else {}
        rows: list[dict[str, Any]] = []
        for node in self.graph.all_nodes():
            if not node.is_valid_at(now):
                continue  # 已到期的旧事实不参与现在的检索
            if scopes is not None and node.scope not in scopes:
                continue  # 别的项目的记忆不该出现在这个项目的候选里
            sem = sem_scores.get(node.name, 0.0)
            tag = len(wanted & set(node.tags)) / max(1, len(wanted)) if wanted else 0.0
            rec = min(1.0, max(0.0, node.effective_weight(now)))
            total = (
                cfg.semantic_alpha * sem
                + cfg.semantic_tag_beta * tag
                + cfg.semantic_recency_gamma * rec
            )
            rows.append(
                {
                    "name": node.name,
                    "score": round(total, 6),
                    "semantic": round(sem, 6),
                    "tag": round(tag, 6),
                    "recency": round(rec, 6),
                }
            )
        rows.sort(key=lambda r: (-r["score"], r["name"]))
        return rows

    def score_breakdown(
        self,
        query: str,
        query_tags: tuple[str, ...] = (),
        top_k: int | None = None,
        scopes: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        """为什么这条记忆被召回？三路分量逐一列出，不让人对着一个数字猜。"""
        with self._lock:
            rows = self._score_components(query, query_tags, self.now(), scopes)
            return rows[:top_k] if top_k else rows

    def recall(
        self,
        names: list[str] | None = None,
        query_tags: tuple[str, ...] = (),
        limit: int = 8,
        query: str | None = None,
        min_score: float | None = None,
        as_of: float | None = None,
        scopes: set[str] | None = None,
    ) -> list[MemoryNode]:
        """三种入口：精确名字 / 语义查询 / 标签。语义与标签可叠加为混合打分。

        `as_of` 回看某个时刻「当时哪些记忆为真」—— 双时态查询。
        默认只返回现在有效的：一条已经被取代的旧偏好，不该和新的并排出现。
        """
        with self._lock:
            if self._txn_depth == 0:
                self.refresh()
            now = self.now()
            moment = now if as_of is None else float(as_of)
            retrieval = "names"
            top_score = 0.0
            decisive: list[str] = []

            if names is not None:
                # 同一节点在名单里出现多次，只能强化一次。
                hits = [self.graph.get(n) for n in dict.fromkeys(names)]
                hits = [n for n in hits if n is not None]
            else:
                text = (query or "").strip()
                gate = self._config.semantic_min_score if min_score is None else min_score
                if not text and not query_tags:
                    # 无查询条件 = 按显著度召回，等价于「现在最容易被想起的是什么」。
                    # 这里才用得上记忆类型偏向：没有具体目标时，手上的与身份性的先浮上来。
                    retrieval = "salience"
                    live = [
                        n for n in self.graph.all_nodes()
                        if n.is_valid_at(moment)
                        and (scopes is None or n.scope in scopes)
                    ]
                    ranked = sorted(
                        live,
                        key=lambda n: (-n.effective_weight(moment) * self.memory.bias(n), n.name),
                    )[:limit]
                    hits = ranked
                    top_score = (
                        round(hits[0].effective_weight(moment) * self.memory.bias(hits[0]), 6)
                        if hits
                        else 0.0
                    )
                else:
                    retrieval = "semantic" if text else "tags"
                    rows = self._score_components(text, query_tags, moment, scopes)
                    # 「过了门槛」还不够：必须真的有时近之外的理由。
                    # 总分里含 recency，刚写进来的任何东西都能靠它过线 ——
                    # 那不是相关，是新鲜。语义全零、标签全空时应当召回不到。
                    above = [
                        r for r in rows
                        if r["score"] >= gate and (r["semantic"] > 0 or r["tag"] > 0)
                    ]
                    # 相对门槛：低于最高分一半的命中只是「字面沾边」，一并挡掉。
                    floor = above[0]["score"] * self._config.semantic_relative_floor if above else 0.0
                    kept = [r for r in above if r["score"] >= floor][:limit]
                    hits = [self.graph.get(r["name"]) for r in kept]
                    hits = [n for n in hits if n is not None]
                    top_score = kept[0]["score"] if kept else 0.0
                    # 语义「独有贡献」：去掉语义分量就过不了门槛 —— 这才是语义真的在干活。
                    for r in kept:
                        without_sem = r["score"] - self._config.semantic_alpha * r["semantic"]
                        if r["semantic"] > 0 and without_sem < gate <= r["score"]:
                            decisive.append(r["name"])

            if not hits:
                self._log(
                    "miss",
                    {"query_tags": list(query_tags), "query": query, "retrieval": retrieval},
                    now,
                )
                return []

            hit_names = [n.name for n in hits]

            # 顺理成章：想起一件事，沿着逻辑关系想起另一件。
            # 这条路和语义余弦不是一回事 —— 它读的是「推得出」，不是「长得像」。
            spread_hits: list[str] = []
            if len(self.logic) and self._config.logic_max_hops > 0:
                activation = self.logic.spread(
                    hit_names,
                    max_hops=self._config.logic_max_hops,
                    hop_decay=self._config.logic_hop_decay,
                )
                extra = sorted(
                    (
                        (act, name)
                        for name, act in activation.items()
                        if act >= self._config.logic_spread_threshold and name not in hit_names
                    ),
                    key=lambda pair: (-pair[0], pair[1]),
                )
                room = max(0, limit - len(hits))
                for _act, name in extra[:room]:
                    node = self.graph.get(name)
                    if node is not None:
                        hits.append(node)
                        spread_hits.append(name)
                hit_names = [n.name for n in hits]

            for node in hits:
                node.access(now, self._config.access_gain)
            self.graph.coactivate(hit_names, now)
            self._log(
                "recall",
                {
                    "names": hit_names,
                    "retrieval": retrieval,
                    "top_score": top_score,
                    "semantic_only": decisive,
                    "spread_hits": spread_hits,
                    "core_hits": [n.name for n in hits if n.tier in ("core", "fact")],
                },
                now,
            )
            self._maybe_save()
            return hits

    @_mutating("observe_style")
    def observe_style(
        self,
        key: str,
        statement: str,
        signal: float,
        weight: float = 1.0,
        opposes: tuple[str, ...] = (),
        pinned_by_user: bool = False,
    ) -> None:
        with self._lock:
            self.style.observe(
                key, statement, signal, self.now(), weight, opposes, pinned_by_user
            )
            self._maybe_save()

    def active_style(self) -> list[str]:
        """风格固化的落地点：只有真正生效的条目才能影响对外输出。"""
        with self._lock:
            return self.style.active_statements(self.now())

    def adjudications(self, limit: int = 10) -> list[dict[str, Any]]:
        """裁决史：每一次「谁赢、为什么赢、差多少」都可回溯。"""
        with self._lock:
            return [a.to_dict() for a in self.style.adjudication_log[-limit:]]

    @_mutating("pin_style")
    def pin_style(self, key: str, statement: str = "") -> None:
        """人工裁决入口：钉死一条，系统据此抑制与它对立的条目。证据不动。"""
        with self._lock:
            self.style.observe(
                key, statement or key, +1.0, self.now(), weight=0.0, pinned_by_user=True
            )
            self._log("style_pin", {"key": key}, self.now())
            self._maybe_save()

    # ---------- 基础人格：宪法层，系统只读 ----------

    @_mutating("found")
    def found(
        self,
        traits: dict[str, dict[str, Any]] | None = None,
        axioms: tuple[str, ...] | None = None,
        author: str = "user",
        seal: bool = False,
        replace: bool = True,
    ) -> Foundation:
        """用户构建基础人格。这是**唯一**能凭空造出基座的入口。

        replace=False 时在现有基座上加维度（用于渐进构建），否则整体替换。
        """
        with self._lock:
            now = self.now()
            if traits is None:
                foundation = default_foundation(now, author)
                # 只给公理不给维度，是最常见的用法；这里必须真的生效，不能静默吞掉
                if axioms is not None:
                    foundation.axioms = tuple(axioms)
                foundation.sealed = seal
            else:
                specs: dict[str, TraitSpec] = {}
                for key, raw in traits.items():
                    if isinstance(raw, TraitSpec):
                        specs[key] = raw
                    else:
                        known = {
                            k: v
                            for k, v in raw.items()
                            if k in dataclass_field_names(TraitSpec)
                        }
                        known.setdefault("label", key)
                        known.setdefault("baseline", 0.5)
                        specs[key] = TraitSpec(**known)
                merged = {} if replace else dict(self.foundation.traits)
                merged.update(specs)
                foundation = Foundation(
                    version=1 if replace else self.foundation.version + 1,
                    created_at=now if replace else self.foundation.created_at,
                    author=author,
                    axioms=tuple(
                        axioms
                        if axioms is not None
                        else (() if replace else self.foundation.axioms)
                    ),
                    traits=merged,
                    amendments=(
                        [] if replace else list(self.foundation.amendments)
                    ),
                    sealed=seal,
                )
            foundation.validate()
            self.foundation = foundation
            self.personality.rebuild(foundation)
            self._log(
                "found",
                {
                    "version": foundation.version,
                    "author": author,
                    "traits": len(foundation.traits),
                    "axioms": len(foundation.axioms),
                    "replaced": replace,
                },
                now,
            )
            self._maybe_save()
            return foundation

    @_mutating("amend_foundation")
    def amend_foundation(
        self,
        reason: str,
        traits: dict[str, dict[str, Any]] | None = None,
        axioms: tuple[str, ...] | None = None,
        author: str = "user",
        force: bool = False,
    ) -> Foundation:
        """修宪。必须给理由；封印状态下必须显式 force —— 让「改基础」永远是一次郑重动作。"""
        with self._lock:
            if not reason.strip():
                raise ValueError("修改基础人格必须给出理由")
            if self.foundation.sealed and not force:
                raise PermissionError("基础人格已封印，需显式 force 才能修改")
            now = self.now()
            changes: list[dict[str, Any]] = []
            new_specs = dict(self.foundation.traits)
            payload = dict(traits or {})
            removals = list(payload.pop(_REMOVE, []) or [])
            for key, raw in payload.items():
                old = new_specs.get(key)
                spec = (
                    raw
                    if isinstance(raw, TraitSpec)
                    else TraitSpec(
                        **{
                            k: v
                            for k, v in raw.items()
                            if k in dataclass_field_names(TraitSpec)
                        }
                    )
                )
                if old is not None and old.locked and spec.floor < old.floor:
                    raise PermissionError(f"{key} 已锁定，不得下调下界")
                changes.append(
                    {
                        "key": key,
                        "from": old.to_dict() if old else None,
                        "to": spec.to_dict(),
                    }
                )
                new_specs[key] = spec
            for key in removals:
                if key not in new_specs:
                    raise KeyError(f"要删除的维度不存在: {key}")
                if new_specs[key].locked:
                    raise PermissionError(f"{key} 已锁定，不得删除")
                changes.append(
                    {"key": key, "from": new_specs.pop(key).to_dict(), "to": None}
                )
            foundation = Foundation(
                version=self.foundation.version + 1,
                created_at=self.foundation.created_at,
                author=author,
                axioms=tuple(axioms if axioms is not None else self.foundation.axioms),
                traits=new_specs,
                amendments=list(self.foundation.amendments)
                + [
                    Amendment(
                        version=self.foundation.version + 1,
                        at=now,
                        author=author,
                        reason=reason,
                        changes=changes,
                    )
                ],
                sealed=self.foundation.sealed,
            )
            foundation.validate()
            self.foundation = foundation
            self.personality.rebuild(foundation)
            self._log(
                "amend_foundation",
                {"version": foundation.version, "reason": reason, "changes": changes},
                now,
            )
            self._maybe_save()
            return foundation

    def foundation_status(self) -> dict[str, Any]:
        with self._lock:
            f = self.foundation
            return {
                "version": f.version,
                "author": f.author,
                "sealed": f.sealed,
                "axioms": list(f.axioms),
                "traits": {k: v.to_dict() for k, v in f.traits.items()},
                "amendments": [a.to_dict() for a in f.amendments],
            }

    # ---------- 人格：逐步构筑 ----------

    @_mutating("observe_trait")
    def observe_trait(
        self, key: str, signal: float, weight: float = 1.0
    ) -> dict[str, Any]:
        """一条证据推动人格一格。返回位移账目（含是否被基座包络挡回）。"""
        with self._lock:
            now = self.now()
            result = self.personality.observe(key, signal, now, weight)
            if result["applied"] and result["delta"]:
                self._log("trait_shift", result, now)
            elif not result["applied"]:
                self._log("trait_blocked", result, now)
            self._maybe_save()
            return result

    def trait_values(self) -> dict[str, float]:
        with self._lock:
            return self.personality.values()

    def drift(self) -> dict[str, Any]:
        """人格离基座多远、走得多快。距离与速度是两个轴，不能合成一个数。"""
        with self._lock:
            return drift_report(
                self.foundation, self.personality, self.now(), self._config
            )

    # ---------- 人设：建立、完善、变更 ----------

    @_mutating("define_persona")
    def define_persona(
        self,
        key: str,
        name: str,
        role: str,
        voice: dict[str, float] | None = None,
        emphasis: tuple[str, ...] = (),
        suppress: tuple[str, ...] = (),
        boundaries: tuple[str, ...] = (),
        author: str = "user",
    ) -> Persona:
        with self._lock:
            now = self.now()
            persona = Persona(
                key=key,
                name=name,
                role=role,
                voice={k: float(v) for k, v in (voice or {}).items()},
                emphasis=tuple(emphasis),
                suppress=tuple(suppress),
                boundaries=tuple(boundaries),
                author=author,
                created_at=now,
                updated_at=now,
            )
            for dim in list(persona.voice) + list(persona.emphasis) + list(persona.suppress):
                if dim not in self.personality.traits:
                    raise KeyError(f"人设引用了人格里不存在的维度: {dim}")
            self.persona.define(persona, now)
            self._log(
                "persona_define",
                {"key": key, "revision": persona.revision, "role": role},
                now,
            )
            self._maybe_save()
            return persona

    @_mutating("activate_persona")
    def activate_persona(
        self, key: str, reason: str = "", author: str = "user"
    ) -> PersonaRevision:
        """换一张面孔。理由会被记下 —— 无理由频繁切换会被 churn 判据抓出来。"""
        with self._lock:
            now = self.now()
            revision = self.persona.activate(key, now, reason, author)
            self._log("persona_activate", revision.to_dict(), now)
            self._maybe_save()
            return revision

    @_mutating("rollback_persona")
    def rollback_persona(self, rev: int, author: str = "user") -> PersonaRevision:
        with self._lock:
            now = self.now()
            revision = self.persona.rollback(rev, now, author)
            self._log("persona_rollback", revision.to_dict(), now)
            self._maybe_save()
            return revision

    def persona_history(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            return [r.to_dict() for r in self.persona.revisions[-limit:]]

    def persona_voice(self) -> dict[str, Any]:
        """人设的落地点：此刻该用什么口径说话。没有这一层，人设就只是记账。"""
        with self._lock:
            return self.persona.voice(self.personality)

    def suggest_persona(self) -> dict[str, Any]:
        """完善人设的两条路：改人设求诚实，或喂证据求成长。系统不替人选。"""
        with self._lock:
            return self.persona.suggest(self.personality)

    def persona_status(self) -> dict[str, Any]:
        with self._lock:
            now = self.now()
            active = self.persona.active()
            drift = drift_report(self.foundation, self.personality, now, self._config)
            consistency = self.persona.consistency(self.personality)
            churn = self.persona.churn(now, self._config.reflection_window_days)
            return {
                "foundation_version": self.foundation.version,
                "foundation_sealed": self.foundation.sealed,
                "traits": self.personality.values(),
                "drift": drift,
                "active_persona": active.to_dict() if active else None,
                "personas": sorted(self.persona.personas),
                "revisions": len(self.persona.revisions),
                "consistency": consistency,
                "churn": churn,
            }

    # ---------- 三个引擎的内部版本（供 run_cycle 复用） ----------

    def _style_cycle_unlocked(self, now: float) -> list[Event]:
        produced = self.style.run_cycle(now)
        self.events.extend(produced)
        self._trim_events()
        return produced

    def _consolidate_unlocked(self, now: float) -> ConsolidationResult:
        result = self.consolidation.run(self.graph, now)
        if result.created:
            # 沉淀造出的结论必须能立刻被检索到，但**不能逐条 upsert**：
            # upsert 会置脏，而脏标记的语义是「下次检索前全量重建」，
            # 一个簇一条 → 一次 run_cycle 里就是 8 次全量重建（实测占了 85% 的时间）。
            # 这里只补缺失（O(k)），把「语料统计变了」留给批末统一对齐一次。
            self.semantics._fill_missing(self.graph)
            self.semantics.mark_dirty()
        for name in result.created:
            node = self.graph.get(name)
            if node is not None:
                node.kind = "semantic"  # 沉淀出来的结论属于语义记忆
        self.events.extend(result.events)
        self._trim_events()
        return result

    def _grasp_unlocked(self, now: float) -> list[dict[str, Any]]:
        log = self.value.grasp(self.graph, now, self.logic.support_map())
        self._last_grasp = log
        if log:
            self._log(
                "grasp",
                {
                    "grabbed": [row["name"] for row in log],
                    "spent": round(sum(row["spent"] for row in log), 4),
                },
                now,
            )
        return log

    def _gc_unlocked(self, now: float) -> list[str]:
        """遗忘变成一次有理由的取舍：每条记忆都拿到判决 + 理由 + 四个分量。

        两步，且顺序不可换：
          1. 先结算注意力 —— 工作记忆拿不住的、被挤出的，一律沉为情景记忆；
          2. 再判价值 —— 类型规则优先于价值判断：身份记忆再轻也不参与 GC，
             「我是谁」不是一条可以被时间冲掉的事实，它只能被修宪改写。
        """
        demoted = self.memory.evict_working(self.graph, now, self._config.forget_w)
        if demoted:
            self._log(
                "memory_demote",
                {"count": len(demoted), "names": [d["name"] for d in demoted]},
                now,
            )
        # 失效的记忆不按价值判：它的去留由「历史保留期」决定。
        # 否则一条曾经很重要的旧偏好会靠高重要性永远赖着，检索就会不停翻旧账。
        retired = {n.name: n for n in self.graph.all_nodes() if n.is_retired()}
        horizon = self._config.history_retention_days * DAY
        expired = [
            name
            for name, node in retired.items()
            # 钉死是用户主权：`gc_decisions` 里它拿的是 pinned_by_user，永远不会被价值判掉。
            # 历史回收若绕过这一条，就等于「钉死的记忆在被取代 90 天后自己消失了」——
            # 两条规则互相矛盾时，应该让更强的那个（用户显式钉死）赢。
            if node.valid_to is not None
            and not node.pinned
            and now - float(node.valid_to) >= horizon
        ]
        decisions = self.value.gc_decisions(self.graph, now, self.logic.support_map())
        victims: list[str] = list(expired)
        protected: list[str] = []
        for name in expired:
            self._log(
                "history_expire",
                {
                    "name": name,
                    "valid_to": float(retired[name].valid_to or 0.0),
                    "kept_days": round((now - float(retired[name].valid_to or now)) / DAY, 2),
                },
                now,
            )
        for d in decisions:
            if d["name"] in retired:
                d["verdict"] = "retired"
                d["reason"] = "superseded_by:" + (retired[d["name"]].superseded_by or "?")
                continue
            if d["verdict"] != "forgotten":
                continue
            node = self.graph.get(d["name"])
            kind = (node.kind or classify(node)) if node is not None else "episodic"
            if not kind_spec(kind).forgettable:
                d["verdict"] = "kept"
                d["reason"] = "kind_protected"
                protected.append(d["name"])
            else:
                victims.append(d["name"])
        self.last_gc = decisions
        for name in victims:
            node = self.graph.get(name)
            if node is not None:
                self.tombstone[name] = node
        self.graph.remove_nodes_from(victims)
        for name in victims:
            self.semantics.forget(name)  # 幽灵向量会让检索命中已死记忆
        pruned = self.logic.prune(victims)  # 节点没了，指向它的断言也必须消失
        self._bump_logic()
        self._log(
            "gc",
            {
                "deleted": victims,
                "scanned": len(self.graph) + len(victims),
                "reasons": {
                    d["name"]: d["reason"] for d in decisions if d["verdict"] == "forgotten"
                },
                "protected_by_kind": protected,
                "pruned_relations": [list(p) for p in pruned],
            },
            now,
        )
        return victims

    # ---------- 自述 ----------

    def _reliability_map(self) -> dict[str, float]:
        """可靠度 = 情绪造成的编码失真 × 争议折价。它不会随时间恢复。"""
        contested = self.logic.contested()
        out: dict[str, float] = {}
        for node in self.graph.all_nodes():
            base = node.affect.fidelity(self._config.affect_distortion)
            penalty = 1.0 - 0.25 * contested.get(node.name, 0.0)
            out[node.name] = round(max(0.0, base * penalty), 6)
        return out

    def _narrate_unlocked(self, now: float) -> NarrativeRevision:
        assessment = self.assessment(now)
        revision = self.narrative.compose(
            self.graph,
            assessment,
            reliability=self._reliability_map(),
            contested=self.logic.contested(),
            now=now,
        )
        self.narrative.apply_centrality(self.graph, revision, now)
        self._log(
            "narrate",
            {
                "rev": revision.rev,
                "selected": len(revision.selected),
                "dropped": len(revision.dropped),
                "coherence": revision.coherence,
                "reliability": revision.reliability,
                "monopoly": revision.monopoly,
            },
            now,
        )
        return revision

    def _persona_cycle_unlocked(self, now: float) -> dict[str, Any]:
        """人格结算：只观测与记账，绝不自动改基座，也绝不自动换人设。

        自动「纠正」漂移等于替用户决定他是谁 —— 这一步只报警，不动手。
        """
        report = drift_report(self.foundation, self.personality, now, self._config)
        consistency = self.persona.consistency(self.personality)
        churn = self.persona.churn(now, self._config.reflection_window_days)
        health = (report["health"], consistency["consistent"], churn["unstable"])
        if health != self._last_persona_health:
            self._log(
                "persona_health",
                {
                    "health": report["health"],
                    "distance": report["distance"],
                    "consistent": consistency["consistent"],
                    "oversell": consistency["oversell"],
                    "churn_unstable": churn["unstable"],
                },
                now,
            )
            self._last_persona_health = health
        return {"drift": report, "consistency": consistency, "churn": churn}

    def _memory_cycle_unlocked(self, now: float) -> list[dict[str, Any]]:
        """工作记忆退场已并入 GC 第一步（顺序不可拆），此处只做只读回放。"""
        return list(self.memory.last_evictions)

    def _selfcorrect_unlocked(self, now: float, report: ReflectionReport) -> dict[str, Any]:
        """自修正一轮：先验旧账，再结新账。

        顺序不能反：正在验证的修正必须先结算，否则新修正会叠在未验证的假设上。
        """
        metrics = dict(report.metrics)
        self.corrector.observe(metrics, now)  # 先攒样本，判决只读观察窗之后的
        settled = self.corrector.verify(metrics, now, metrics_at=report.generated_at)
        candidates, escalations = self.corrector.detect(metrics, now)
        self.corrector.escalations = escalations
        proposed = self.corrector.propose(candidates, metrics, now)
        applied = [p for p in proposed if self.corrector.apply(p)]
        produced = self.corrector.events(settled + applied, now)
        if produced:
            self.events.extend(produced)
            self._trim_events()
        if escalations:
            self._log(
                "selfcorrect_escalate",
                {"items": [e.to_dict() for e in escalations]},
                now,
            )
        return {"settled": settled, "applied": applied, "escalations": escalations}

    def _reflect_unlocked(self, now: float) -> ReflectionReport:
        assessment = self.assessment(now)
        dist = self.value.distribution(assessment)
        struct = self.structural(now)
        head = self.narrative.head
        grasp_log = self._last_grasp
        grabbed = {row["name"] for row in (grasp_log or [])}
        candidates = [
            n
            for n in self.graph.all_nodes()
            if not n.pinned and n.tier != "trace" and not n.consolidated_into
        ]
        ungrabbed_share = (
            1.0 - len(grabbed & {n.name for n in candidates}) / len(candidates)
            if (candidates and grasp_log is not None)
            else 0.0
        )
        report = self.reflection.run(
            self.graph,
            self.style,
            self.events,
            self.tombstone,
            now,
            context={
                "logic_edges": len(self.logic),
                "contested": len(self.logic.contested()),
                "entropy": dist["entropy"],
                "entropy_health": self.value.health(dist),
                "top_share": dist["top_share"],
                "top3_share": dist["top3_share"],
                "structure": struct,
                "structure_health": self.value.structure_health(struct),
                "ungrabbed_share": round(ungrabbed_share, 6),
                "narrative_coherence": head.coherence if head else None,
                "narrative_reliability": head.reliability if head else None,
                "narrative_monopoly": head.monopoly if head else None,
                "foundation_version": self.foundation.version,
                "persona_drift": drift_report(
                    self.foundation, self.personality, now, self._config
                ),
                "persona_consistency": self.persona.consistency(self.personality),
                "persona_churn": self.persona.churn(
                    now, self._config.reflection_window_days
                ),
            },
        )
        if head is not None:
            report.narrative = f"{report.narrative}｜{head.text}"
        self.last_report = report.to_dict()
        self._log(
            "reflect",
            {
                "findings": len(report.findings),
                "suggestions": len(report.suggestions),
                "narrative": report.narrative,
            },
            now,
        )
        return report

    # ---------- 对外引擎入口 ----------

    @_mutating("consolidate")
    def consolidate(self) -> ConsolidationResult:
        with self._lock:
            result = self._consolidate_unlocked(self.now())
            self._maybe_save()
            return result

    @_mutating("gc")
    def gc(self) -> list[str]:
        with self._lock:
            victims = self._gc_unlocked(self.now())
            self._maybe_save()
            return victims

    @_mutating("reflect")
    def reflect(self) -> ReflectionReport:
        with self._lock:
            report = self._reflect_unlocked(self.now())
            self._maybe_save()
            return report

    @_mutating("self_correct")
    def self_correct(self) -> dict[str, Any]:
        """手动触发一次自修正。只动白名单内的派生参数，基座与人设一律不碰。"""
        with self._lock:
            now = self.now()
            if self.last_report is None:
                report = self._reflect_unlocked(now)
            else:
                report = ReflectionReport(
                    generated_at=now,
                    window_days=self._config.reflection_window_days,
                    metrics=dict(self.last_report.get("metrics", {})),
                    findings=list(self.last_report.get("findings", [])),
                    suggestions=list(self.last_report.get("suggestions", [])),
                    narrative=str(self.last_report.get("narrative", "")),
                )
            result = self._selfcorrect_unlocked(now, report)
            self._maybe_save()
            return {
                "applied": [p.to_dict() for p in result["applied"]],
                "settled": [p.to_dict() for p in result["settled"]],
                "escalations": [e.to_dict() for e in result["escalations"]],
            }

    def correction_status(self) -> dict[str, Any]:
        with self._lock:
            return self.corrector.status()

    def memory_composition(self) -> dict[str, Any]:
        with self._lock:
            return self.memory.describe(self.graph)

    @_mutating("run_cycle")
    def run_cycle(self) -> dict[str, Any]:
        """一轮完整的认知代谢。返回摘要，不抛异常吞状态。"""
        with self._lock:
            now = self.now()
            style_events = self._style_cycle_unlocked(now)
            cons = self._consolidate_unlocked(now)
            grasp_log = self._grasp_unlocked(now)
            demoted: list[dict[str, Any]] = []
            victims = self._gc_unlocked(now)
            demoted = self._memory_cycle_unlocked(now)
            revision = self._narrate_unlocked(now)
            persona_state = self._persona_cycle_unlocked(now)
            report = self._reflect_unlocked(now)
            correction = self._selfcorrect_unlocked(now, report)
            # 批末对齐：沉淀可能改了语料（多了结论节点），IDF 是所有向量共用的尺子，
            # 在这里重建**一次**，比在每个簇后面各重建一次便宜一个量级，
            # 而且保证「这一轮结束后索引与图严格一致」—— 不留冷查询给下一个调用方。
            self.semantics.sync(self.graph)
            persisted = self._maybe_save()
            # 配置被自修正改过，但没落盘 → 趁还在本函数里，把它退回去。
            # 否则磁盘上是旧值、内存里是新值，重启静默回滚，而这一轮的行为
            # 已经按新值跑完了 —— 没有任何人会收到报错。
            rolled_back: list[str] = []
            if not persisted and correction["applied"]:
                for patch in correction["applied"]:
                    if self.corrector.rollback_unpersisted(patch):
                        rolled_back.append(patch.pid)
                self._log(
                    "selfcorrect_unpersisted",
                    {"rolled_back": rolled_back, "path": self._path},
                    now,
                )
            return {
                "at": now,
                "style_events": [e.kind for e in style_events],
                "consolidated": cons.created,
                "grasped": [row["name"] for row in grasp_log],
                "demoted": [d["name"] for d in demoted],
                "forgotten": victims,
                "nodes": len(self.graph),
                "retired": sum(1 for n in self.graph.all_nodes() if n.is_retired()),
                "narrative": revision.to_dict(),
                "persona": {
                    "health": persona_state["drift"]["health"],
                    "distance": persona_state["drift"]["distance"],
                    "consistent": persona_state["consistency"]["consistent"],
                },
                "selfcorrect": {
                    "applied": [p.pid for p in correction["applied"]],
                    "rolled_back": rolled_back,
                    "settled": [f"{p.pid}:{p.status}" for p in correction["settled"]],
                    "escalated": [e.code for e in correction["escalations"]],
                },
                "report": report.to_dict(),
            }

    @_mutating("restore")
    def restore(self, name: str) -> MemoryNode | None:
        """误删可回滚：从墓碑复活，并给记忆一次完整的重生权重。"""
        with self._lock:
            node = self.tombstone.pop(name, None)
            if node is None:
                return None
            now = self.now()
            node.base_weight = max(1.0, node.base_weight)
            node.last_accessed = now
            node.access_count += 1
            self.memory.enroll(node)
            self.graph.add_node(node)
            self.semantics.upsert(node)
            self._log("restore", {"name": name}, now)
            self._maybe_save()
            return node

    @_mutating("apply_suggestion")
    @_mutating("apply_suggestion")
    def apply_suggestion(self, param: str, value: Any) -> Any:
        """显式采纳反思建议。三个引擎共享同一 config 对象，改一处即全局生效。

        「存不下就不算改过」在这里也必须成立：v0.9 只处理了自修正路径上的
        落盘失败回滚，手动采纳这条路上 `_maybe_save()` 的返回值是被丢掉的 ——
        磁盘满/权限问题会让内存用新阈值跑、重启后静默回滚，且没有任何日志。
        现在落盘失败就把参数退回去并抛错，让调用方知道这次没成。
        """
        with self._lock:
            fields = dataclass_field_names(PalaceConfig)
            if param not in fields:
                raise KeyError(f"未知参数: {param}")
            old = getattr(self._config, param)
            setattr(self._config, param, value)
            try:
                self._config.validate()
            except ValueError:
                setattr(self._config, param, old)  # 拒绝非法值并回滚，不留半坏配置
                raise
            self._log("config_change", {"param": param, "from": old, "to": value})
            if not self._maybe_save():
                setattr(self._config, param, old)
                self._log(
                    "config_reverted",
                    {"param": param, "to": old, "reason": "save_failed"},
                )
                raise StoreError(
                    f"参数 {param} 未能落盘，已回滚为 {old!r}（存不下就不算改过）"
                )
            return old

    # ---------- 快照与持久化 ----------

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            if self._txn_depth == 0:
                self.refresh()
            now = self.now()
            graph_nodes = self.graph.all_nodes()
            tiers = Counter(n.tier for n in graph_nodes)
            assessment = self.assessment(now)
            dist = self.value.distribution(assessment)
            struct = self.structural(now)
            head = self.narrative.head
            return {
                "now": now,
                "nodes": len(graph_nodes),
                "edges": len(self.graph.edges()),
                "tiers": dict(tiers),
                "tombstone": len(self.tombstone),
                "events": len(self.events),
                "active_style": self.style.active_statements(now),
                "style_rules": sorted(self.style.rules),
                "style_conflicts": [list(p) for p in self.style.conflicts(now)],
                "active_style_conflicts": [
                    list(p) for p in self.style.active_conflicts(now)
                ],
                "pending_style_conflicts": self.style.pending_conflicts(now),
                "adjudications": len(self.style.adjudication_log),
                "semantic_indexed": len(self.semantics),
                "semantic_ghosts": self.semantics.ghosts(self.graph),
                "logic_edges": len(self.logic),
                "logic_ghosts": [list(g) for g in self.logic.ghosts(self.graph.nodes())],
                "contested": sorted(self.logic.contested()),
                "entropy": dist["entropy"],
                "attention": {
                    "top_share": dist["top_share"],
                    "top3_share": dist["top3_share"],
                    "effective_n": dist["effective_n"],
                },
                "entropy_health": self.value.health(dist),
                "structure": struct,
                "structure_health": self.value.structure_health(struct),
                "semantic_learned": {k: list(v) for k, v in self.semantics.learned.items()},
                "durability": {**dict(WRITE_STATS), "last_save_ok": self.last_save_ok},
                "self_model_revision": head.rev if head else 0,
                "narrative": head.to_dict() if head else None,
                "narrative_stability": self.narrative.stability_of_story(),
                "foundation": {
                    "version": self.foundation.version,
                    "sealed": self.foundation.sealed,
                    "axioms": list(self.foundation.axioms),
                },
                "personality": self.personality.values(),
                "persona_drift": drift_report(
                    self.foundation, self.personality, now, self._config
                ),
                "persona": {
                    "active": self.persona.active_key,
                    "available": sorted(self.persona.personas),
                    "revisions": len(self.persona.revisions),
                    "voice": self.persona.voice(self.personality),
                    "consistency": self.persona.consistency(self.personality),
                    "churn": self.persona.churn(
                        now, self._config.reflection_window_days
                    ),
                },
                "memory_model": self.memory.describe(self.graph),
                "selfcorrect": {
                    "applied": sum(1 for p in self.corrector.patches if p.status == "kept"),
                    "pending": [p.pid for p in self.corrector.patches if p.status == "pending"],
                    "reverted": sum(
                        1 for p in self.corrector.patches if p.status == "reverted"
                    ),
                    "blocked": sorted(self.corrector.blocked),
                    "escalations": [e.code for e in self.corrector.escalations],
                },
                "last_report": self.last_report,
            }

    def _blobs(self) -> dict[str, Any]:
        """小体积的引擎状态：整体替换，不做行级差分（它们本来就是整体对象）。"""
        return {
            "config": self._config.to_dict(),
            "logic": self.logic.to_dict(),
            "style": self.style.to_dict(),
            "narrative": self.narrative.to_dict(),
            "foundation": self.foundation.to_dict(),
            "personality": self.personality.to_dict(),
            "persona": self.persona.to_dict(),
            "selfcorrect": self.corrector.to_dict(),
            "last_report": self.last_report,
        }

    def _state_payload(self) -> dict[str, Any]:
        """完整快照（JSON 后端、导出、备份用）。"""
        payload = self._blobs()
        payload["version"] = STATE_VERSION
        payload["saved_at"] = self.now()
        payload["graph"] = self.graph.to_dict()
        payload["events"] = [e.to_dict() for e in self.events]
        payload["tombstone"] = [n.to_dict() for n in self.tombstone.values()]
        return payload

    def _delta_payload(self) -> dict[str, Any]:
        """增量提交：只带「自上次落盘以来变过的」节点与事件。

        为什么值得单独一条路径：改一条记忆却序列化整个库，是「扩展性」这句话
        在实现层面的真实含义。这里只把脏节点交给 store，其余行一个字节都不碰。
        """
        dirty: list[dict[str, Any]] = []
        for node in self.graph.all_nodes():
            if getattr(node, "_dirty", True) or node.name not in self._persisted_nodes:
                dirty.append(node.to_dict())
        current = set(self.graph.nodes())
        removed = sorted(self._persisted_nodes - current)
        if len(self.events) >= self._event_cursor:
            fresh_events = self.events[self._event_cursor :]
        else:
            # 事件被 trim 过，游标失效：整段重发，靠 (ts,kind) 唯一索引去重
            fresh_events = list(self.events)
        edges = (
            [[a, b, e.to_dict()] for a, b, e in self.graph.edges()]
            if getattr(self.graph, "_edge_dirty", True) or removed
            else None
        )
        return {
            "version": STATE_VERSION,
            "saved_at": self.now(),
            "blobs": self._blobs(),
            "nodes": dirty,
            "removed": removed,
            "edges": edges,
            "events": [e.to_dict() for e in fresh_events],
            "tombstone": [n.to_dict() for n in self.tombstone.values()],
        }

    def _after_persist(self) -> None:
        """提交成功后才更新游标与脏标记 —— 顺序反了就会「以为存过了」。"""
        self._persisted_nodes = set(self.graph.nodes())
        self._event_cursor = len(self.events)
        for node in self.graph.all_nodes():
            if getattr(node, "_dirty", False):
                node._dirty = False
        self.graph._edge_dirty = False

    def _save_unlocked(self, path: str | None = None, reason: str = "save") -> bool:
        """返回「是否已持久化」。"""
        target = path or self._path
        if not target:
            raise ValueError("未指定持久化路径")
        if self._store is not None and os.path.abspath(target) == self._store.path:
            if getattr(self._store, "supports_delta", False):
                result = self._store.save(
                    self._delta_payload(),
                    reason=reason,
                    delta=True,
                    base_revision=self.revision,
                )
            else:
                result = self._store.save(self._state_payload(), reason=reason)
            self.revision = result.revision
            self.state_id = result.state_id
            self.last_save_result = result.to_dict()
            if result.rebased:
                self.conflict_count += result.rebased
            self._after_persist()
            self._path = target
            return result.durable
        # 写到别的路径 = 复制一份库到那儿（`save as`），**不切换**当前库。
        # 切换的语义会让「拿一份副本来对比」顺手把主库的写入路径也改掉 ——
        # 之后所有自动保存都写进副本，主库从此不再更新，而没有任何报错。
        copy = open_store(target, backend=self._config.backend)
        copy.save(self._state_payload(), reason="copy")
        if hasattr(copy, "close"):
            copy.close()  # type: ignore[attr-defined]
        return True

    def save(self, path: str | None = None, reason: str = "save") -> str:
        """显式落盘。

        注意：它写的是**内存里现在这份**，不会先跟磁盘合并。
        多进程同时改同一个库时请用 autosave=True —— 那样每次修改都在事务里提交
        （先重载、再改、再写），才不会把别人的提交整段盖掉。
        """
        with self._lock:
            target = path or self._path
            if not target:
                raise ValueError("未指定持久化路径")
            self._retry_locked(lambda: self._save_unlocked(target, reason=reason), "save")
            return target

    REQUIRED_STATE_KEYS: tuple[str, ...] = (
        "version",
        "config",
        "graph",
        "logic",
        "style",
        "narrative",
        "foundation",
        "personality",
        "persona",
        "selfcorrect",
        "events",
        "tombstone",
        "last_report",
    )

    @classmethod
    def _validate_state(cls, state: Any, path: str) -> None:
        """损坏的快照绝不能被当成「空记忆系统」安静地继续跑。"""
        if not isinstance(state, dict):
            raise ValueError(f"快照顶层不是对象: {path}")
        if state.get("version") not in SUPPORTED_STATE_VERSIONS:
            raise ValueError(
                f"快照版本不支持: {state.get('version')!r}"
                f"（支持 {list(SUPPORTED_STATE_VERSIONS)}）@ {path}"
            )
        missing = [k for k in cls.REQUIRED_STATE_KEYS if k not in state]
        if missing:
            raise ValueError(f"快照缺少顶层键 {missing} @ {path}")
        graph = state["graph"]
        if not isinstance(graph, dict) or "nodes" not in graph or "edges" not in graph:
            raise ValueError(f"快照 graph 结构损坏 @ {path}")

    def _load_unlocked(
        self, path: str, config_override: PalaceConfig | None = None
    ) -> None:
        if self._store is not None and os.path.abspath(path) == self._store.path:
            try:
                result = self._store.load(validate=lambda s: self._validate_state(s, path))
            except StoreEmpty:
                # 库文件在、但还没有任何提交（另一个进程刚建好）。这就是空库，
                # 不是损坏 —— 保持默认状态，第一笔写入会把它初始化。
                return
            state = result.payload
            self.revision = result.revision
            self.state_id = result.state_id
            self.recovered = result.recovered
        else:
            state = read_json(path)
            self._validate_state(state, path)
            if isinstance(state.get("payload"), dict):  # 别人递来的信封格式
                state = state["payload"]
        self._restore_state(state, path, config_override)

    def _restore_state(
        self, state: dict[str, Any], path: str, config_override: PalaceConfig | None
    ) -> None:
        # 严格校验只对「本版快照」生效：v4/v5/v6 快照缺的是本版新增字段，
        # 用默认值补齐才是正确的迁移方向；反过来，v7 快照里出现未知字段
        # 说明是别的版本写的，必须当场拒绝 —— 两种错误不能混为一谈。
        cfg = PalaceConfig.from_dict(
            state["config"], strict=int(state.get("version", 0)) >= STATE_VERSION
        )
        if config_override is not None:
            # 快照是权威，但显式传入的 config 代表用户的覆写意图，不能被静默吞掉。
            override = config_override.to_dict()
            for key in dataclass_field_names(PalaceConfig):
                setattr(cfg, key, override[key])
            cfg.validate()
        self._config = cfg
        self.graph = MemoryGraph.from_dict(state["graph"])
        self.logic = LogicGraph.from_dict(state.get("logic"), valid=self.graph.nodes())
        self.style = StyleEngine.from_dict(state["style"], cfg)
        self.consolidation = ConsolidationEngine(
            cfg, self._summarizer, vector_lookup=self._vector_of
        )
        if getattr(cfg, "llm_provider", "") and self.llm is None:
            self.llm = make_reflector(cfg)
        hook = self.llm.as_callable() if self.llm is not None else self.reflection.reflector
        self.reflection = ReflectionEngine(cfg, reflector=hook)
        self.affect = AffectEngine(cfg)
        self.value = ValueModel(cfg)
        self.narrative = NarrativeEngine.from_dict(state.get("narrative"), cfg)
        # 顺序不能反：人格的包络由基座给出，人设的维度合法性由人格给出。
        self.foundation = Foundation.from_dict(state["foundation"])
        self.personality = PersonalityEngine.from_dict(
            state.get("personality"), cfg, self.foundation
        )
        self.persona = PersonaEngine.from_dict(state.get("persona"), cfg)
        self._last_persona_health = None
        # 这两个引擎持有 config 引用，必须用新加载的 cfg 重建，否则原子替换配置后它们还在改旧对象
        self.memory = MemoryModel(cfg)
        self.corrector = SelfCorrector.from_dict(state.get("selfcorrect"), cfg)
        self.semantics.config = cfg  # 词典学习参数跟着新配置走
        # 向量不入盘，必须按内容重建 + 重学词典。但**不在这里重建**：
        # 建索引要编码全库（1000 条约 37ms），而 `status` / `view` / 只追加的 `remember`
        # 根本不用它。置脏即可 —— 所有语义入口（scores / _score_components /
        # _vector_of / _find_duplicate / explain）进门前都会先 sync()。
        self.semantics.mark_dirty()
        self.events = [Event.from_dict(e) for e in state["events"]]
        restored = [MemoryNode.from_dict(n) for n in state["tombstone"]]
        self.tombstone = {n.name: n for n in restored}
        self.last_report = state["last_report"]
        self._path = path
        # 刚从磁盘读上来的东西不是「脏」的 —— 不重置的话，下一次 save 会把整个库再写一遍。
        self._persisted_nodes = set(self.graph.nodes())
        self._event_cursor = len(self.events)
        for node in self.graph.all_nodes():
            node._dirty = False
        self.graph._edge_dirty = False

    def load(self, path: str | None = None) -> None:
        with self._lock:
            self._load_unlocked(path or self._path)

    def __enter__(self) -> "MemoryPalace":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._path:
            self.save()
        self.close()

    def close(self) -> None:
        """释放存储句柄。

        Windows 上必须显式关：SQLite 连接不关，文件就被占用，
        删库/搬库/测试清理都会报 PermissionError —— 而错误出现在**下一步**，
        离真正的原因很远。
        """
        if self._store is not None and hasattr(self._store, "close"):
            self._store.close()  # type: ignore[attr-defined]
