"""demo.py - 完整剧本 + 自测。运行：python -m memory_palace.demo

这一版的立场：一个测不出问题的测试套件，比没有测试更危险。
因此除了业务断言，还包含
  · 时钟真的推进下的幂等性（第 1 幕，孪生账本对照）
  · 语义检索与语义沉淀，标签零重叠也能召回/结晶（第 2.5 幕）
  · 对立风格的强制裁决与可逆性（第 3、3.5 幕）
  · 配置的逐字段/逐类型往返与坏快照拒绝（第 5 幕，16 种坏数据）
  · 并发下 access_count 的精确增量（第 7 幕）
  · 原子写的故障注入（第 9 幕）
  · 元测试：故意注入坏实现，验证断言确实会变红（第 10、16 幕）
  · 七个旧局限的定点清除，每条都配突变审计（第 21 幕）

哲学层（第 12–15 幕）对应原句里被代码漏掉的四个词：
  · 情感权重 → affect：潮水会退、刻痕不退、情绪让记忆更难忘也更不可靠
  · 逻辑关联 → logic：共现不是凭据，相似不是推出，矛盾会顺着推理链传播
  · 选择     → value：重要性四项合成、熵的健康区间、固定预算、可审计的放手
  · 自我叙事 → narrative：投影而非节点，讲什么反过来决定记得住什么
"""
from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
import time
import warnings
from contextlib import nullcontext
from unittest import mock

from .affect import Affect, Appraisal, appraise_text
from ._console import ensure_utf8_console
from .core import MemoryNode, MutableClock, PalaceConfig
from .logic import LogicGraph, Relation
from .memory_model import MemoryKind
from .narrative import NarrativeEngine
from .palace import MemoryPalace
from .persona import PersonaEngine, PersonalityEngine, TraitSpec
from .semantics import ConceptEncoder
from .store import StoreError, remove_store_files
from .style import StyleEngine
from .value import ValueModel
from . import affect as affect_mod
from . import core as core_mod
from . import memory_model as mm_mod
from . import palace as palace_mod
from . import persona as persona_mod
from . import selfcorrect as sc_mod


def check(label: str, condition: bool, detail: str = "") -> None:
    mark = "PASS" if condition else "FAIL"
    suffix = f"  · {detail}" if detail else ""
    print(f"  [{mark}] {label}{suffix}")
    if not condition:
        raise AssertionError(label)


def weights(palace: MemoryPalace, now: float) -> dict[str, float]:
    return {
        n.name: round(n.effective_weight(now), 6)
        for n in palace.graph.all_nodes()
    }


def config_dict(palace: MemoryPalace) -> dict:
    return palace._config.to_dict()


def config_types_match(a: dict, b: dict) -> bool:
    return a.keys() == b.keys() and all(type(a[k]) is type(b[k]) for k in a)


def idempotence_ok(palace: MemoryPalace, now: float, rounds: int = 6) -> bool:
    """抽成可复用探测器，供第 10 幕的元测试注入坏实现。"""
    before = weights(palace, now)
    deleted: list[str] = []
    for _ in range(rounds):
        deleted.extend(palace.gc())
    return weights(palace, now) == before and not deleted


def main() -> None:
    ensure_utf8_console()
    clock = MutableClock(1_800_000_000.0)
    tmp = tempfile.gettempdir()
    path = os.path.join(tmp, "memory_palace_demo.json")
    bad_path = os.path.join(tmp, "memory_palace_bad.json")
    fault_path = os.path.join(tmp, "memory_palace_fault.json")
    auto_path = os.path.join(tmp, "memory_palace_autosave.json")
    twin_path = os.path.join(tmp, "memory_palace_twin.json")
    for f in (path, bad_path, fault_path, auto_path, twin_path):
        remove_store_files(f)

    cfg = PalaceConfig(
        consolidate_min_group=3,
        consolidate_min_shared_tags=2,
        consolidate_similarity=0.5,
        consolidate_min_weight=0.2,
        style_crystallize_confidence=0.70,
        style_min_observations=3,
        style_min_span_days=1.0,
        style_decrystallize_confidence=0.50,
    )

    print("\n=== 第 0 幕：造一批碎片、一条钉死的硬规则 ===")
    with MemoryPalace(path=path, clock=clock, config=cfg) as p:
        p.remember(
            "rule:no-absolute", "禁止绝对化措辞",
            tags=("硬规则", "措辞"), tier="core", pinned=True,
        )
        for i in range(4):
            p.remember(f"a{i+1}", f"复盘记录 {i+1}", tags=("复盘", "仓位", "凯利"))
        for i in range(3):
            p.remember(f"b{i+1}", f"板块资金观察 {i+1}", tags=("板块", "资金接力", "选股"))
        for i in range(2):
            p.remember(f"c{i+1}", f"输出偏好记录 {i+1}", tags=("输出", "表格", "措辞"))

        for _ in range(3):
            p.recall(names=["a1", "a2", "a3", "a4"])
            p.recall(names=["b1", "b2", "b3"])
            p.recall(names=["c1", "c2"])
            clock.advance(0.2)
        clock.advance(2.0)
        print(f"  初始节点 {len(p.graph)} 个，共现边 {len(p.graph.edges())} 条")

        print("\n=== 第 1 幕：幂等性 —— 时钟必须真的走，管家连巡 18 次 ===")
        p.save(twin_path)
        twin = MemoryPalace(path=twin_path, clock=clock, config=cfg)
        check("孪生账本与本体同态", weights(twin, clock()) == weights(p, clock()))
        pinned_names = {n.name for n in p.graph.all_nodes() if n.pinned}
        series: list[dict[str, float]] = []
        for step in range(6):
            clock.advance(0.25)  # 每轮推进 6 小时：冻结时钟下的幂等只是恒等函数
            before = weights(p, clock())
            deleted: list[str] = []
            for _ in range(3):
                deleted.extend(p.gc())
            after = weights(p, clock())
            series.append(after)
            check(
                f"第 {step+1} 轮 · 同一时刻连巡 3 次，权重逐位不变且零误删",
                after == before and not deleted,
                f"t+{(step+1)*0.25:.2f}天",
            )
        check(
            "巡检 18 次 vs 一次都不巡，期末权重完全一致",
            weights(twin, clock()) == series[-1],
            f"{len(series[-1])} 个节点逐位相同",
        )
        moving = [n for n in series[-1] if n not in pinned_names]

        check(
            "时间确实在推进：非钉死节点权重严格下降",
            bool(moving) and all(series[-1][n] < series[0][n] for n in moving),
            f"{len(moving)} 个节点全部衰减",
        )
        check(
            "钉死节点恒不衰减",
            all(series[-1][n] == series[0][n] for n in pinned_names),
            f"{len(pinned_names)} 条硬规则",
        )

        print("\n=== 第 2 幕：沉淀、风格结算、反思（一轮完整代谢）===")
        p.run_cycle()
        consolidated = [n for n in p.graph.all_nodes() if n.origin == "consolidated"]
        check(
            "碎片恰好压成 2 个高层记忆（不是 ≥2）",
            len(consolidated) == 2,
            str(sorted(n.name for n in consolidated)),
        )
        traces = [n for n in p.graph.all_nodes() if n.tier == "trace"]
        check(
            "沉淀单向门：恰好 7 条碎片降级 trace 且全部指向核心",
            len(traces) == 7 and all(n.consolidated_into for n in traces),
            f"{len(traces)} 条碎片已降级",
        )
        check(
            "两成员组（c1,c2）未达分组下限，未被强行沉淀",
            all(n.tier != "trace" for n in p.graph.all_nodes() if n.name.startswith("c")),
        )
        check("硬规则未被 GC 清扫", "rule:no-absolute" in p.graph)
        check(
            "反思报告不是空壳：指标、发现、叙述三样都得到",
            p.last_report is not None
            and len(p.last_report["metrics"]) >= 15
            and len(p.last_report["findings"]) >= 1
            and len(p.last_report["narrative"]) > 30,
            f"metrics={len(p.last_report['metrics']) if p.last_report else 0}",
        )
        check("自我模型已写入", p.snapshot()["self_model_revision"] == 1)

        print("\n=== 第 2.5 幕：语义检索 —— 标签毫无重叠，内容相关也得召回 ===")
        p.remember("sem:pos", "半凯利仓位：单笔不超过总资金三分之一", tags=("交易口径",))
        p.remember("sem:noise", "周末带娃去公园晒太阳", tags=("琐事",))
        check(
            "索引覆盖全部节点、无幽灵向量",
            p.snapshot()["semantic_indexed"] == p.snapshot()["nodes"]
            and p.snapshot()["semantic_ghosts"] == [],
            f"{p.snapshot()['semantic_indexed']} 条向量",
        )
        breakdown = {r["name"]: r for r in p.score_breakdown("凯利下注比例怎么算")}
        check(
            "语义分量只在内容相关处才亮（不是标签在冒名）",
            breakdown["sem:pos"]["semantic"] > 0.3 and breakdown["sem:pos"]["tag"] == 0.0,
            f"sem={breakdown['sem:pos']['semantic']} tag={breakdown['sem:pos']['tag']}",
        )
        check(
            "无关记忆的语义分远低于相关记忆",
            breakdown["sem:noise"]["semantic"] < breakdown["sem:pos"]["semantic"] / 3,
            f"noise={breakdown['sem:noise']['semantic']} vs pos={breakdown['sem:pos']['semantic']}",
        )
        hits = [n.name for n in p.recall(query="凯利下注比例怎么算")]
        check("标签为 0 重叠的记忆仍被召回", "sem:pos" in hits, str(hits))
        check("无关记忆没被拉进来", "sem:noise" not in hits)
        sem_recall = [e for e in p.events if e.kind == "recall" and e.payload.get("retrieval") == "semantic"]
        check(
            "召回事件记录了语义独有贡献（可审计）",
            sem_recall and "sem:pos" in sem_recall[-1].payload.get("semantic_only", []),
            str(sem_recall[-1].payload.get("semantic_only") if sem_recall else None),
        )
        p.age("sem:noise", 200)
        p.gc()
        check(
            "被遗忘的记忆同时从索引中移除（无幽灵向量）",
            "sem:noise" in p.tombstone and p.snapshot()["semantic_ghosts"] == [],
        )

        def build_synonyms() -> MemoryPalace:
            pal = MemoryPalace(clock=MutableClock(clock()))
            for name, text in (
                ("f1", "半凯利仓位：单笔不超过三分之一"),
                ("f2", "资金管理下注比例：单笔不超三分之一"),
                ("f3", "凯利公式定仓位，单笔不超三分之一"),
            ):
                pal.remember(name, text, tags=(name,), tier="event")
            return pal

        blind = build_synonyms()
        blind.consolidation.vector_lookup = None  # 退回纯标签口径
        blind_result = blind.consolidate()
        sem_result = build_synonyms().consolidate()
        check(
            "纯标签口径：同义碎片各自为政（3 个孤立分量，零沉淀）",
            blind_result.created == [] and blind_result.clusters_seen == 3,
            f"created={blind_result.created} seen={blind_result.clusters_seen}",
        )
        check(
            "语义口径：同一批碎片被结晶成 1 条",
            len(sem_result.created) == 1 and sem_result.by_semantics == 1,
            f"created={sem_result.created} by_semantics={sem_result.by_semantics}",
        )
        note = next(
            e for e in sem_result.events if e.payload.get("target") == sem_result.created[0]
        )
        check(
            "结晶事件记录了走的是哪条通道（可审计）",
            note.payload.get("via") == "semantics",
            str(note.payload),
        )

        print("\n=== 第 3 幕：风格固化 + 对立裁决 —— 对立方不得同时生效 ===")
        for _ in range(4):
            p.observe_style("prefer_tables", "输出优先表格化", +1, weight=1.0)
            p.observe_style("concise", "输出尽量精简", +1, weight=1.2)
            p.observe_style("verbose", "输出充分展开", +1, weight=1.2, opposes=("concise",))
            p.recall(query_tags=("复盘", "仓位"))
            clock.advance(0.5)
        p.run_cycle()
        active = p.active_style()
        check("风格固化后真正生效", "输出优先表格化" in active, f"生效 {active}")

        pair = p.snapshot()["style_conflicts"]
        check("对立对已被识别", ["concise", "verbose"] in pair, str(pair))
        verdict = p.adjudications(limit=1)[0]
        check(
            "证据势均力敌 → 判「悬而未决」，两条一起停用",
            verdict["reason"] == "ambiguous" and verdict["winner"] is None,
            f"margin={verdict['margin']} reason={verdict['reason']}",
        )
        check(
            "对立方没有同时进入生效输出",
            not ("输出尽量精简" in active and "输出充分展开" in active),
            str(active),
        )
        pending = p.snapshot()["pending_style_conflicts"]
        check(
            "悬而未决被显式上报，而不是静默丢弃",
            {x["key"] for x in pending} == {"concise", "verbose"}
            and all(x["reason"] == "ambiguous" for x in pending),
            str([(x["key"], x["reason"]) for x in pending]),
        )
        check(
            "不变量：生效集合与冲突集合永不相交",
            p.snapshot()["active_style_conflicts"] == [],
        )

        for _ in range(3):
            p.observe_style("verbose", "输出充分展开", +1, weight=1.5, opposes=("concise",))
            clock.advance(0.2)
        p.run_cycle()
        active = p.active_style()
        verdict = p.adjudications(limit=1)[0]
        check(
            "证据拉开差距后由强者独揽，弱者被抑制",
            verdict["winner"] == "verbose" and "输出充分展开" in active
            and "输出尽量精简" not in active,
            f"winner={verdict['winner']} margin={verdict['margin']}",
        )
        check(
            "抑制关系被显式记账（不是悄悄不输出）",
            p.style.rules["concise"].suppressed_by == "verbose",
        )
        check("裁决后仍无并存冲突", p.snapshot()["active_style_conflicts"] == [])
        check("裁决落地后不再有悬而未决条目", p.snapshot()["pending_style_conflicts"] == [])

        for _ in range(6):
            p.observe_style("verbose", "输出充分展开", -1, weight=1.6, opposes=("concise",))
            clock.advance(0.2)
        p.run_cycle()
        check(
            "裁决不是终审：强者被反证击落后，被压制的一方自动复活",
            "输出充分展开" not in p.active_style()
            and "输出尽量精简" in p.active_style()
            and p.style.rules["concise"].suppressed_by is None,
            str(p.active_style()),
        )

        print("\n=== 第 3.5 幕：人工裁决优先，双钉死则双双停用并大声上报 ===")
        cclock = MutableClock(clock())
        pc = MemoryPalace(clock=cclock)
        for _ in range(3):
            pc.observe_style("short", "回复要短", +1, weight=3.0)
            pc.observe_style("long", "回复要长", +1, weight=1.0, opposes=("short",))
            cclock.advance(0.5)
        pc.run_cycle()
        check("证据强者先独揽", pc.active_style() == ["回复要短"], str(pc.active_style()))
        pc.pin_style("long", "回复要长")
        check(
            "用户钉死后，证据较弱的一方反转胜出",
            "回复要短" not in pc.active_style() and "回复要长" in pc.active_style(),
            str(pc.active_style()),
        )
        check(
            "钉死裁决的理由可追溯",
            pc.adjudications(limit=1)[0]["reason"] == "user_pinned",
            str(pc.adjudications(limit=1)[0]),
        )
        pc.pin_style("short", "回复要短")
        pending = {x["key"]: x["reason"] for x in pc.snapshot()["pending_style_conflicts"]}
        check(
            "双方都被钉死 → 系统拒绝替人裁决，两条同时停用",
            pc.active_style() == [] and pc.snapshot()["active_style_conflicts"] == [],
            str(pc.active_style()),
        )
        check(
            "自相矛盾被大声上报，而不是静默取一个",
            pending.get("short") == "user_conflict" and pending.get("long") == "user_conflict",
            str(pending),
        )

        print("\n=== 第 4 幕：反证 —— 已固化的风格也能被推翻 ===")
        for _ in range(5):
            p.observe_style("prefer_tables", "输出优先表格化", -1, weight=1.6)
            clock.advance(0.3)
        forget_w_before = p._config.forget_w
        p.run_cycle()
        check("反证累积后撤销固化", "输出优先表格化" not in p.active_style())
        check("撤销次数被记账", p.style.rules["prefer_tables"].flip_count >= 1)
        check("反思只提建议，不擅自改参数", p._config.forget_w == forget_w_before)
        check("自我模型迭代而非分裂", p.snapshot()["self_model_revision"] >= 3)
        orphaned = [
            n
            for n in p.graph.all_nodes()
            if n.origin == "consolidated"
            and any(m not in p.graph for m in n.consolidated_from)
        ]
        check(
            "碎片被 GC 后，结晶仍保留来源名单（溯源不随碎片一起蒸发）",
            bool(orphaned)
            and all(n.content.startswith("[沉淀]") for n in orphaned),
            f"{len(orphaned)} 个结晶的碎片已不在图中",
        )

    print("\n=== 第 5 幕：config 往返 —— 逐字段、逐类型、坏数据必须响亮拒绝 ===")
    # 故意偏离默认值：否则「保存时丢掉 config、加载时补默认」也能骗过逐字段断言。
    p.apply_suggestion("forget_w", 0.023)
    p.apply_suggestion("max_events", 4321)
    p.save(path)
    expected_cfg = config_dict(p)
    check(
        "测试用的 config 确实偏离默认值（防止空胜利）",
        expected_cfg != PalaceConfig().to_dict(),
        f"forget_w={expected_cfg['forget_w']} / max_events={expected_cfg['max_events']}",
    )
    type_mutated = dict(expected_cfg)
    type_mutated["max_events"] = float(type_mutated["max_events"])
    check("类型探测器能抓到 int→float 静默降级", config_types_match(expected_cfg, type_mutated) is False)
    expected_w = weights(p, clock())
    expected_events = len(p.events)
    expected_rules = len(p.style.rules)

    p2 = MemoryPalace(path=path, clock=clock)
    got_cfg = config_dict(p2)
    check("config 逐字段一致", got_cfg == expected_cfg, f"{len(expected_cfg)} 个字段")
    check("config 逐类型一致（int 不许悄悄变 float）", config_types_match(expected_cfg, got_cfg))
    check("节点权重逐位一致", weights(p2, clock()) == expected_w, f"{len(expected_w)} 个节点")
    check("事件日志无损", len(p2.events) == expected_events, f"{expected_events} 条事件")
    check("风格证据链无损", len(p2.style.rules) == expected_rules)
    check("反思报告随快照恢复", p2.last_report is not None)
    # 索引改为按需构建（读库时只置脏）：这里必须先 sync 一次再比对，
    # 因为真正的不变量是「用过之后索引与图严格一致」，不是「加载瞬间就建好」。
    # 顺带把这条断言变强：不光数量对得上，每条向量的内容也要能重建出来。
    p2.semantics.sync(p2.graph)
    snap2 = p2.snapshot()
    check(
        "语义索引按内容重建，不依赖入盘向量（sync 后与图严格一致）",
        snap2["semantic_indexed"] == snap2["nodes"]
        and snap2["semantic_ghosts"] == []
        and all(p2.semantics.sparse(n.name) is not None for n in p2.graph.all_nodes()),
        f"{snap2['semantic_indexed']} 条向量 / {snap2['nodes']} 个节点",
    )
    probe_q = "凯利下注比例怎么算"
    check(
        "跨快照的语义检索结果逐条一致",
        [n.name for n in p.recall(query=probe_q)] == [n.name for n in p2.recall(query=probe_q)],
    )
    shared = (
        p2.consolidation.config is p2._config
        and p2.style.config is p2._config
        and p2.reflection.config is p2._config
    )
    check("恢复后三引擎仍共享同一 config 对象", shared)

    custom = PalaceConfig(forget_w=0.001, consolidate_min_group=9)
    p3 = MemoryPalace(path=path, clock=clock, config=custom)
    check(
        "显式传入的 config 不再被快照静默吞掉",
        p3._config.forget_w == 0.001 and p3._config.consolidate_min_group == 9,
        f"forget_w={p3._config.forget_w} / min_group={p3._config.consolidate_min_group}",
    )

    good_state = json.loads(open(path, "rb").read())
    bad_cases: list[tuple[str, object]] = [
        ("未知配置键", lambda d: d["config"].update({"totally_new_key": 1})),
        ("缺少配置键", lambda d: d["config"].pop("forget_w")),
        ("配置类型错误", lambda d: d["config"].__setitem__("forget_w", "abc")),
        ("配置含 NaN", lambda d: d["config"].__setitem__("forget_w", float("nan"))),
        ("阈值越界", lambda d: d["config"].__setitem__("consolidate_similarity", 1.7)),
        ("整数字段被降级", lambda d: d["config"].__setitem__("max_events", 5000.5)),
        ("悬空边", lambda d: d["graph"]["edges"].append(["ghost", "none", {"weight": 1.0, "coactivations": 1, "last_coactivated": 0.0}])),
        ("节点权重 NaN", lambda d: d["graph"]["nodes"][0].__setitem__("base_weight", float("nan"))),
        ("缺少顶层键", lambda d: d.pop("events")),
        ("版本不匹配", lambda d: d.__setitem__("version", 99)),
        (
            "悬空关系",
            lambda d: d["logic"]["edges"].append(
                {"src": "ghost", "dst": "none", "kind": "entails", "strength": 0.5,
                 "since": 0.0, "observations": 1}
            ),
        ),
        (
            "未知关系类型",
            lambda d: d["logic"]["edges"].append(
                {"src": d["graph"]["nodes"][0]["name"], "dst": d["graph"]["nodes"][1]["name"],
                 "kind": "feels_like", "strength": 0.5, "since": 0.0, "observations": 1}
            ),
        ),
        (
            "关系强度越界",
            lambda d: d["logic"]["edges"].append(
                {"src": d["graph"]["nodes"][0]["name"], "dst": d["graph"]["nodes"][1]["name"],
                 "kind": "causal", "strength": 1.7, "since": 0.0, "observations": 1}
            ),
        ),
        ("情感效价越界", lambda d: d["graph"]["nodes"][0]["affect"].__setitem__("valence", 3.0)),
        (
            "情感含 NaN",
            lambda d: d["graph"]["nodes"][0]["affect"].__setitem__("peak_arousal", float("nan")),
        ),
        ("缺少叙事段", lambda d: d.pop("narrative")),
    ]
    rejected_count = 0
    for label, mutate in bad_cases:
        broken = copy.deepcopy(good_state)
        mutate(broken)  # type: ignore[operator]
        with open(bad_path, "w", encoding="utf-8") as f:
            json.dump(broken, f, ensure_ascii=False)
        try:
            MemoryPalace(path=bad_path, clock=clock)
            rejected = False
        except (ValueError, TypeError, KeyError, StoreError):
            rejected = True
        rejected_count += int(rejected)
        check(f"坏快照被响亮拒绝 · {label}", rejected)
    check("全部坏快照均未被静默接受", rejected_count == len(bad_cases), f"{rejected_count}/{len(bad_cases)}")

    print("\n=== 第 6 幕：误删可回滚 ===")
    p2.remember("expired:1", "一条早该忘掉的临时记忆", tags=("临时",), tier="ephemeral")
    p2.age("expired:1", 60)
    victims = p2.gc()
    check("过期记忆被清理并进墓碑", "expired:1" in victims and "expired:1" in p2.tombstone)
    restored = p2.restore("expired:1")
    check("可以从墓碑复活", restored is not None and "expired:1" in p2.graph)

    print("\n=== 第 7 幕：并发 —— 精确核对 access_count，而不是只看「没报错」 ===")
    safe = ["rule:no-absolute"] + [
        n.name for n in p2.graph.all_nodes() if n.tier == "core"
    ][:3]
    unique_safe = list(dict.fromkeys(safe))
    baseline = {
        n: (p2.graph.get(n).access_count, p2.graph.get(n).base_weight)
        for n in unique_safe
    }
    errors: list[str] = []
    nodes_before = len(p2.graph)
    THREADS, ROUNDS = 8, 25
    print(f"  名单含重复项: {len(safe) != len(set(safe))}，去重后 {len(unique_safe)} 个节点")

    def worker(_: int) -> None:
        try:
            for _ in range(ROUNDS):
                p2.recall(names=safe)
                p2.gc()
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    deltas = []
    for name in unique_safe:
        node = p2.graph.get(name)
        expected = baseline[name][0] + THREADS * ROUNDS
        got = node.access_count if node else -1
        deltas.append((name, got, expected))
        check(
            f"access_count 精确增量 · {name}",
            got == expected,
            f"baseline={baseline[name][0]} 实际={got} 应为={expected}",
        )
    check("并发无异常", not errors, f"{len(errors)} 个异常")
    check("safe 节点全部存活且节点数稳定", all(d[1] > 0 for d in deltas) and len(p2.graph) == nodes_before)
    check(
        "权重上限未被突破",
        all(n.base_weight <= MemoryNode.MAX_W for n in p2.graph.all_nodes()),
    )
    check(
        "并发未丢更新：base_weight 单调不减",
        all(p2.graph.get(n).base_weight >= baseline[n][1] for n in unique_safe),
    )
    check(
        "last_accessed 全部推进到当前时刻",
        all(p2.graph.get(n).last_accessed == clock() for n in unique_safe),
    )

    print("\n=== 第 8 幕：显式采纳建议，且改动必须能持久化 ===")
    suggestions = (p2.last_report or {}).get("suggestions", [])
    if suggestions:
        s = suggestions[0]
        old = p2.apply_suggestion(s["param"], s["suggested"])
        check("显式采纳后三引擎同步", getattr(p2.style.config, s["param"]) == s["suggested"], f"{old} → {s['suggested']}")
        p2.save(path)
        p4 = MemoryPalace(path=path, clock=clock)
        check(
            "被采纳的配置改动随快照往返",
            getattr(p4._config, s["param"]) == s["suggested"],
            f"{s['param']}={getattr(p4._config, s['param'])}",
        )
    before_illegal = p2._config.forget_w
    try:
        p2.apply_suggestion("forget_w", "非法值")
        illegal_rejected = False
    except ValueError:
        illegal_rejected = True
    check(
        "非法参数值被拒绝且不留半坏配置",
        illegal_rejected and p2._config.forget_w == before_illegal,
        f"回滚后 forget_w={p2._config.forget_w}",
    )

    print("\n=== 第 9 幕：原子性故障注入 —— 旧快照必须完好、临时文件必须清干净 ===")
    # 故障注入要一个**独占**目录：放在公共临时目录里的话，
    # 别人（或上一次运行）留下的 .palace-*.tmp 会被算到自己头上，测出来的是噪声。
    fault_dir = os.path.join(tmp, "memory_palace_faults")
    os.makedirs(fault_dir, exist_ok=True)
    for stale in os.listdir(fault_dir):
        os.unlink(os.path.join(fault_dir, stale))
    fault_path = os.path.join(fault_dir, "memory_palace_fault.json")
    p2.save(fault_path)
    good_bytes = open(fault_path, "rb").read()

    def leftovers() -> list[str]:
        return [
            f for f in os.listdir(fault_dir)
            if f.startswith(".palace-") and f.endswith(".tmp")
        ]

    class _Torn:
        """写一半就断电的假文件对象：验证「临时文件坏了也不许污染旧快照」。"""

        def __init__(self, real) -> None:
            self._real = real

        def __enter__(self):
            return self

        def __exit__(self, *exc) -> bool:
            self._real.close()
            return False

        def __getattr__(self, name):
            return getattr(self._real, name)

        def write(self, data: str) -> int:
            self._real.write(data[: max(1, len(data) // 2)])
            raise OSError("模拟写入中途断电")

    _real_fdopen = os.fdopen  # 先抓住真身：patch 之后模块里的 os.fdopen 就是它自己了

    def torn_fdopen(fd, *a, **kw):
        return _Torn(_real_fdopen(fd, *a, **kw))

    injections: list[tuple[str, str | None, object, object]] = [
        ("序列化写一半断电", "memory_palace.core.os.fdopen", torn_fdopen, {"version": 99}),
        ("文件 fsync 失败", "memory_palace.core.os.fsync", OSError("fsync 失败"), {"version": 99}),
        ("原子 replace 失败", "memory_palace.core.os.replace", OSError("磁盘满"), {"version": 99}),
        ("不可序列化对象", None, None, {"bad": object()}),
    ]
    for label, target, side_effect, payload in injections:
        before = open(fault_path, "rb").read()
        context = mock.patch(target, side_effect=side_effect) if target else nullcontext()
        with context:
            try:
                core_mod.atomic_write_json(fault_path, payload)
                raised = "无"
            except Exception as exc:  # noqa: BLE001
                raised = type(exc).__name__
        after = open(fault_path, "rb").read()
        residue = leftovers()
        check(
            f"故障注入 · {label}",
            after == before == good_bytes and not residue,
            f"异常={raised} / 临时文件残留={len(residue)}",
        )
        try:
            recovered = MemoryPalace(path=fault_path, clock=clock)
            loadable = recovered.snapshot()["nodes"] == len(json.loads(good_bytes)["graph"]["nodes"])
        except Exception:  # noqa: BLE001
            loadable = False
        check(f"故障注入 · {label} 旧快照仍可加载", loadable)

    before = open(fault_path, "rb").read()
    with mock.patch("memory_palace.core.os.replace", side_effect=OSError("replace 失败")), mock.patch(
        "memory_palace.core.os.unlink", side_effect=OSError("清理失败")
    ):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            try:
                core_mod.atomic_write_json(fault_path, {"version": 99})
            except OSError:
                pass
    after = open(fault_path, "rb").read()
    residue = leftovers()
    warned = any(issubclass(w.category, RuntimeWarning) for w in caught)
    check(
        "故障注入 · replace 与 tmp 清理同时失败",
        after == before == good_bytes and warned,
        f"残留={len(residue)} / 已发警告={warned}",
    )
    for filename in residue:
        os.unlink(os.path.join(fault_dir, filename))
    check("叠加故障后残留可人工回收", not leftovers())

    new_state = {
        "version": 1,
        "config": expected_cfg,
        "graph": {"nodes": [], "edges": []},
        "style": {"rules": []},
        "events": [],
        "tombstone": [],
        "last_report": None,
    }
    core_mod.atomic_write_json(fault_path, new_state)
    new_bytes = open(fault_path, "rb").read()
    check(
        "对照组：正常写入确实替换旧内容且无残留",
        new_bytes != good_bytes
        and json.loads(new_bytes)["version"] == 1
        and not leftovers(),
    )

    print("\n=== 第 10 幕：测试的测试 —— 故意把实现弄坏，断言必须变红 ===")
    probe = MemoryPalace(clock=MutableClock(1_800_000_000.0))
    probe.remember("m1", "探针节点")
    normal_ok = idempotence_ok(probe, probe.now())
    original_weight = MemoryNode.effective_weight

    def leaky_weight(self, now, affect_gain=None):
        self.base_weight -= 0.02  # 复刻「衰减带副作用」的老 bug
        return original_weight(self, now, affect_gain)

    MemoryNode.effective_weight = leaky_weight
    try:
        broken_ok = idempotence_ok(probe, probe.now())
    finally:
        MemoryNode.effective_weight = original_weight
    check("幂等断言在正常实现下为绿", normal_ok)
    check("幂等断言能抓到被破坏的衰减", broken_ok is False)

    probe2 = MemoryPalace(clock=MutableClock(1_800_000_000.0))
    probe2.remember("n1", "探针节点")
    count_before = probe2.graph.get("n1").access_count
    original_access = MemoryNode.access
    MemoryNode.access = lambda self, now, gain=0.5: None
    try:
        probe2.recall(names=["n1"])
        count_after = probe2.graph.get("n1").access_count
    finally:
        MemoryNode.access = original_access
    check(
        "计数断言能抓到丢更新",
        (count_after == count_before + 1) is False,
        f"坏实现下 {count_before} → {count_after}",
    )

    legacy = MemoryPalace(clock=MutableClock(1_800_000_000.0))
    legacy.remember("x", "探针节点")
    legacy_baseline = legacy.graph.get("x").access_count
    original_recall = MemoryPalace.recall

    def legacy_recall(self, names=None, query_tags=(), limit=8):
        """复刻修复前的实现：名单里的重复项会被强化两次。"""
        with self._lock:
            now = self.now()
            hits = [n for n in (self.graph.get(k) for k in (names or [])) if n is not None]
            for node in hits:
                node.access(now, self._config.access_gain)
            return hits

    MemoryPalace.recall = legacy_recall
    try:
        for _ in range(10):
            legacy.recall(names=["x", "x"])
        legacy_count = legacy.graph.get("x").access_count
    finally:
        MemoryPalace.recall = original_recall
    check(
        "计数断言能抓到重复名单的双重强化",
        (legacy_count == legacy_baseline + 10) is False,
        f"旧实现 {legacy_baseline} → {legacy_count}，应为 {legacy_baseline + 10}",
    )

    t = 1_800_000_000.0

    def build_conflicting() -> StyleEngine:
        engine = StyleEngine(PalaceConfig(style_min_span_days=0.0))
        engine.observe("a", "甲风格", +1, t, weight=5.0, opposes=("b",))
        engine.observe("b", "乙风格", +1, t, weight=5.0, opposes=("a",))
        for rule in engine.rules.values():
            rule.crystallized = True
        return engine

    healthy = build_conflicting()
    healthy.adjudicate(t)
    check("裁决器正常时，冲突对零并存", healthy.active_conflicts(t) == [])

    original_adjudicate = StyleEngine.adjudicate
    StyleEngine.adjudicate = lambda self, now, emit=True: []  # 复刻「没有裁决」的旧架构
    try:
        broken = build_conflicting()
    finally:
        StyleEngine.adjudicate = original_adjudicate
    check(
        "去掉裁决器后，不变量断言必须变红",
        len(broken.active_conflicts(t)) == 1,
        f"坏实现下并存冲突 {len(broken.active_conflicts(t))} 对",
    )

    semantic_engine = MemoryPalace(clock=MutableClock(t))
    semantic_engine.remember("s1", "半凯利仓位管理")
    # 注意 patch 的是 encode_with：检索走的是带 IDF 的那条路径（encode 只是它的无 IDF 包装）。
    # 只 patch encode 会「打空」—— 坏编码器根本没生效，这条审计就变成了假绿。
    # 第一次改造时确实踩了：审计报 FAIL，不是它错了，是它抓到了自己过时。
    original_with = ConceptEncoder.encode_with
    original_encode = ConceptEncoder.encode
    ConceptEncoder.encode_with = lambda self, idf, text: [0.0] * self.dim
    ConceptEncoder.encode = lambda self, text: [0.0] * self.dim
    try:
        blind_hits = semantic_engine.recall(query="凯利仓位怎么分配")
    finally:
        ConceptEncoder.encode_with = original_with
        ConceptEncoder.encode = original_encode
    check(
        "语义检索断言能抓到全零假向量",
        len(blind_hits) == 0,
        f"坏编码器下召回 {len(blind_hits)} 条",
    )

    print("\n=== 第 11 幕：自动保存失败不得炸穿主流程 ===")
    auto = MemoryPalace(path=auto_path, clock=clock, autosave=True)
    with mock.patch("memory_palace.core.os.replace", side_effect=OSError("磁盘满")):
        try:
            auto.remember("auto:x", "写入时落盘失败")
            survived = True
        except OSError:
            survived = False
    check("业务写入未被落盘失败中断", survived and "auto:x" in auto.graph)
    check("失败被显式记账而非静默吞掉", any(e.kind == "save_failed" for e in auto.events))
    auto.remember("auto:y", "故障恢复后的写入")
    check("故障恢复后可继续落盘", json.load(open(auto_path, encoding="utf-8"))["graph"]["nodes"] != [])

    # ================= 哲学层：情感 / 逻辑 / 选择 / 自述 =================
    #
    # 这一层不是「再加几个指标」，而是把原句里被代码漏掉的四个词补上：
    #   情感权重 → affect     逻辑关联 → logic     选择 → value     自我叙事 → narrative

    def affect_scenario(config: PalaceConfig | None = None):
        """同一时刻写下、同样久没被想起，只有情绪强度不同。"""
        clk = MutableClock(1_800_100_000.0)
        pal = MemoryPalace(clock=clk, config=config)
        pal.remember("calm", "一次平常的记录", tags=("复盘",), tier="event")
        pal.remember("hot", "一次强烈的记录", tags=("复盘",), tier="event",
                     valence=-0.9, arousal=0.95)
        base = {n: pal.graph.get(n).base_weight for n in ("calm", "hot")}
        clk.advance(8)
        weight = {n: pal.graph.get(n).effective_weight(clk()) for n in ("calm", "hot")}
        return pal, clk, base, weight

    print("\n=== 第 12 幕：情感 —— 潮水会退，河床不退，而刻痕让记忆失真 ===")
    pe, ce, base_w, w8 = affect_scenario()
    check(
        "写情绪不等于强化：两条记忆初始强度完全相同",
        base_w["calm"] == base_w["hot"] == 1.0,
        f"calm={base_w['calm']} hot={base_w['hot']}",
    )
    check(
        "同样 8 天无人问津：情绪强的活着，平静的掉到物理门槛以下",
        w8["hot"] > 0.05 > w8["calm"],
        f"hot={w8['hot']:.4f} / calm={w8['calm']:.4f}（阈值 0.05）",
    )
    imp_hot = pe.importance_of("hot")
    imp_calm = pe.importance_of("calm")
    check(
        "重要度里能拆出情绪分量，而且平静的那条确实为 0",
        imp_hot["affect"] > 0.3 and imp_calm["affect"] == 0.0,
        f"hot.affect={imp_hot['affect']} calm.affect={imp_calm['affect']}",
    )
    victims = pe.gc()
    reasons = {d["name"]: d["reason"] for d in pe.last_gc}
    check(
        "同一轮 GC 给出不同判决，且理由可读",
        victims == ["calm"] and reasons["hot"] == "valued" and reasons["calm"] == "weight_floor",
        str(reasons),
    )

    pv = MemoryPalace(clock=MutableClock(1_800_100_000.0))
    pv.remember("fall", "一件很痛的事", tags=("复盘",), valence=-0.9, arousal=0.95)
    cv = MutableClock(1_800_100_000.0)
    pv2 = MemoryPalace(clock=cv)
    pv2.remember("fall", "一件很痛的事", tags=("复盘",), valence=-0.9, arousal=0.95)
    cv.advance(8)
    v_now, _a_now = pv2.graph.get("fall").affect.settled(cv())
    peak = pv2.graph.get("fall").affect.peak_arousal
    check(
        "潮水退了：当下感受到的痛已不足当初六成，而刻痕仍是满值",
        abs(v_now) < 0.45 * 0.6 and peak == 0.95,
        f"效价 {v_now:+.3f}（写入时 -0.450）/ 峰值唤醒 {peak}",
    )
    check(
        "情绪让它更难忘，同时让它更不可靠（不是白送好处）",
        pv2.graph.get("fall").affect.fidelity(0.35) < 0.72
        and pv.affect.blank(0.0).fidelity(0.35) == 1.0,
        f"高唤醒可靠度 {pv2.graph.get('fall').affect.fidelity(0.35):.3f} / 平静 1.000",
    )
    before_content = pv2.graph.get("fall").content
    before_base = pv2.graph.get("fall").base_weight
    pv2.reappraise("fall", 0.4)
    after = pv2.graph.get("fall")
    check(
        "重估只改「现在怎么看」，不改事实、不白送一次强化",
        after.content == before_content
        and after.base_weight == before_base
        and after.affect.valence == 0.4
        and after.affect.peak_arousal == 0.95,
        f"valence -0.450 → {after.affect.valence}，base_weight 恒为 {before_base}",
    )

    print("\n=== 第 12.5 幕：逻辑 —— 相似不等于推得出 ===")
    pl = MemoryPalace(clock=MutableClock(1_800_200_000.0))
    pl.remember("s", "单笔不超三分之一", tags=("仓位",))
    pl.remember("e1", "所以最多同时持有三只", tags=("仓位",))
    pl.remember("neg", "可以满仓押单只", tags=("仓位",))
    pl.remember("c1", "另一条也谈仓位", tags=("仓位",))
    pl.recall(names=["s", "c1"])  # 制造共现：两层都记一笔，但含义不同
    pl.assert_relation("s", "e1", Relation.ENTAILS, 0.9)
    pl.assert_relation("s", "neg", Relation.CONTRADICTS, 0.8)
    support = {n: pl.support_of(n) for n in ("s", "e1", "neg", "c1")}
    check(
        "推出关系构成凭据，共现关系不构成凭据",
        support["e1"] > 0.8 and support["c1"] == 0.0,
        f"e1={support['e1']:.2f}（被推出） c1={support['c1']:.2f}（只是常一起出现）",
    )
    check(
        "承重也算凭据，但要打折：能推出别人的不如被别人推出的硬",
        0 < support["s"] < support["e1"],
        f"s={support['s']:.2f} < e1={support['e1']:.2f}",
    )
    closure = pl.consequences("s")
    check(
        "传递闭包：从 s 走两跳拿到 e1，走不到无关的 c1",
        "e1" in closure and "c1" not in closure and 0.4 < closure["e1"] < 0.9,
        f"{ {k: round(v, 3) for k, v in closure.items()} }",
    )
    pairs = pl.logic.contradiction_pairs()
    propagated = [p for p in pairs if p[2] == "propagated"]
    check(
        "矛盾会顺着推理链传播：s 与 neg 冲突，则 s 推出的 e1 也与 neg 冲突",
        any(p[0] == "e1" and p[1] == "neg" for p in propagated),
        str([(a, b, src) for a, b, src, _ in pairs]),
    )
    check(
        "系统只标记争议，不擅自裁决",
        {"neg", "e1"} <= set(pl.logic.contested()) and len(pl.logic.contested()) >= 2,
        str({k: round(v, 3) for k, v in pl.logic.contested().items()}),
    )
    hits = [n.name for n in pl.recall(names=["s"])]
    check(
        "想起 s，顺着逻辑想起 e1；同样相似的 c1 不会被想起",
        "e1" in hits and "c1" not in hits,
        str(hits),
    )
    check(
        "扩散激活被单独记账，与语义命中区分开",
        any("e1" in (e.payload.get("spread_hits") or []) for e in pl.events if e.kind == "recall"),
    )
    pl.assert_relation("c1", "s", Relation.CAUSAL, 0.5)  # 给 c1 一条真实关系，好验证剪枝
    edges_before = len(pl.logic)
    pl.age("c1", 400)
    pl.gc()
    check(
        "节点被遗忘时，指向它的断言一并消失（没有悬空关系）",
        "c1" in pl.tombstone
        and len(pl.logic) == edges_before - 1
        and pl.logic.ghosts(pl.graph.nodes()) == [],
        f"关系 {edges_before} → {len(pl.logic)} 条，悬空 {len(pl.logic.ghosts(pl.graph.nodes()))} 条",
    )

    print("\n=== 第 13 幕：选择 —— 梯度、预算、可审计的放手 ===")
    flat = MemoryPalace(clock=MutableClock(1_800_300_000.0))
    for i in range(10):
        flat.remember(f"u{i}", f"普通记录 {i}", tags=("杂记",))
    dist_flat = flat.entropy()
    check(
        "什么都一样重要 = 什么都不重要（梯度为零）",
        dist_flat["health"] == "flat" and abs(dist_flat["top_share"] - 0.1) < 1e-6,
        str(dist_flat),
    )
    narrow = MemoryPalace(clock=MutableClock(1_800_300_000.0))
    for i in range(10):
        narrow.remember(f"w{i}", f"普通记录 {i}", tags=("杂记",))
        narrow.age(f"w{i}", 20)
    narrow.remember("king", "唯一撑着的那条", tags=("主线",))
    dist_narrow = narrow.entropy()
    check(
        "一切都押在一件事上，同样不是自我（另一端）",
        dist_narrow["health"] == "narrow" and dist_narrow["top_share"] > 0.9,
        str(dist_narrow),
    )

    def grasp_count(n: int) -> tuple[int, float]:
        pal = MemoryPalace(clock=MutableClock(1_800_300_000.0))
        for i in range(n):
            pal.remember(f"x{i}", f"记录 {i}", tags=("杂记",))
        log = pal.value.grasp(pal.graph, pal.now(), {})
        return len(log), round(sum(row["spent"] for row in log), 4)

    small, small_spent = grasp_count(10)
    big, big_spent = grasp_count(40)
    check(
        "预算固定：存量从 10 涨到 40，能抓住的仍然是那几条",
        small == big == 3 and small_spent == big_spent == 3.0,
        f"10 条→抓 {small} 条，40 条→抓 {big} 条，各花 {big_spent}",
    )
    tightened = flat.value.grasp(flat.graph, flat.now(), {}, budget=1.5)
    check(
        "预算可以被外部收紧：给 1.5 就只抓两条，第二条分到半份",
        len(tightened) == 2 and round(sum(r["spent"] for r in tightened), 4) == 1.5,
        str([(r["name"], r["spent"]) for r in tightened]),
    )

    two_law = MemoryPalace(
        clock=MutableClock(1_800_300_000.0), config=PalaceConfig(importance_forget=0.9)
    )
    two_law.remember("frag", "已沉淀的碎片", tags=("复盘",), tier="trace")
    two_law.remember("plain", "没沉淀的普通记录", tags=("复盘",), tier="event")
    two_law.age("frag", 1)
    two_law.age("plain", 1)
    imp_frag = two_law.importance_of("frag")
    imp_plain = two_law.importance_of("plain")
    gone = two_law.gc()
    verdicts = {d["name"]: (d["verdict"], d["reason"]) for d in two_law.last_gc}
    check(
        "同一条价值标准下，两条记忆的账面几乎一样低",
        abs(imp_frag["total"] - imp_plain["total"]) < 0.35
        and imp_plain["total"] < 0.9,
        f"frag={imp_frag['total']:.3f} plain={imp_plain['total']:.3f}",
    )
    check(
        "但结论与碎片适用两套遗忘律：碎片按物理强度退场，结论按价值取舍",
        gone == ["plain"]
        and verdicts["frag"] == ("kept", "delegated_to_core")
        and verdicts["plain"] == ("forgotten", "importance_below_threshold"),
        str(verdicts),
    )
    audited = all(
        {"weight", "affect", "support", "central"} <= set(d)
        and d["reason"] in {
            "pinned_by_user", "weight_floor", "delegated_to_core",
            "importance_below_threshold", "valued",
        }
        for d in two_law.last_gc
    )
    check("每一个放手都有理由、且四个分量都在账上（可审计）", audited, f"{len(two_law.last_gc)} 条判决")

    print("\n=== 第 14 幕：自述 —— 是选择，不是倾倒 ===")
    c14 = MutableClock(1_800_400_000.0)
    pn = MemoryPalace(clock=c14)
    for i in range(6):
        pn.remember(f"n{i}", f"复盘第 {i} 条", tags=("复盘",))
    for i in range(3):
        pn.remember(f"m{i}", f"板块观察 {i}", tags=("板块",))
    pn.remember("trauma", "一次重挫，情绪极强", tags=("复盘",), valence=-0.95, arousal=1.0)
    pn.remember("noise", "随手记的一条", tags=("杂记",))
    pn.remember("old_frag", "早年的原始记录", tags=("复盘",), tier="trace")
    pn.age("noise", 12)
    node_count_before = len(pn.graph)
    rev = pn.tell()
    check(
        "自述是投影，不是图里的一个节点：讲完之后图里没有多出一个「我」",
        "self:reflection" not in pn.graph and len(pn.graph) == node_count_before,
        f"{node_count_before} → {len(pn.graph)}，self 节点存在={('self:reflection' in pn.graph)}",
    )
    check(
        "留下的是少数，放下的是多数，而且每条放下都有理由",
        len(rev.selected) < len(pn.graph)
        and len(rev.dropped) >= 2
        and all(d["reason"] for d in rev.dropped),
        f"留 {len(rev.selected)} / 放 {len(rev.dropped)}",
    )
    check(
        "不值得讲的按价值筛掉，底层碎片则压根不属于叙述层",
        {d["reason"] for d in rev.dropped} >= {"below_threshold", "delegated_fragment"},
        str([(d["name"], d["reason"]) for d in rev.dropped]),
    )
    chap = MemoryPalace(
        clock=MutableClock(1_800_400_000.0), config=PalaceConfig(narrative_max_chapters=2)
    )
    for i, tag in enumerate(("甲", "乙", "丙", "丁", "戊", "己")):
        chap.remember(f"c{i}", f"主题{tag}的一条记录", tags=(tag,))
    rev_chap = chap.tell()
    check(
        "章节超过上限时合并成「其他」，而不是碎成流水账",
        len(rev_chap.chapters) == 2 and any(c.title == "其他" for c in rev_chap.chapters),
        str([(c.arc, c.title, len(c.nodes)) for c in rev_chap.chapters]),
    )
    check(
        "不可靠被标在具体条目上，而不是被平均掉",
        rev.unreliable == ["trauma"] and "细节可能失真" in rev.text,
        f"unreliable={rev.unreliable}",
    )
    check(
        "叙事弧有先后：最早的一章是起源，此刻还在被触碰的是当下",
        {c.arc for c in rev.chapters} <= {"起源", "转折", "当下"}
        and rev.chapters[0].arc in ("起源", "当下"),
        str([(c.arc, c.title) for c in rev.chapters]),
    )
    check(
        "讲过一次就留下叙事地位（被讲进故事的条目确实被记了一笔）",
        all(pn.graph.get(n).narrative_hits >= 1 for n in rev.selected),
        str({n: pn.graph.get(n).narrative_hits for n in rev.selected[:3]}),
    )

    mon = MemoryPalace(clock=MutableClock(1_800_400_000.0))
    for i in range(3):
        mon.remember(f"side{i}", f"边角记录 {i}", tags=("边角",))
    for i in range(6):
        mon.remember(f"main{i}", f"主线记录 {i}", tags=("主线",), valence=0.5, arousal=0.6)
    rev_mon = mon.tell()
    check(
        "自述收窄时会被大声指出来，而不是自我陶醉",
        rev_mon.monopoly > 0.7 and "我的自述正在收窄" in rev_mon.text,
        f"单主题占比 {rev_mon.monopoly}",
    )
    mon.reflect()
    check(
        "反思层也能看见「故事被一个主题垄断」，而不只是盯着删了几条",
        any("垄断" in f for f in mon.last_report["findings"]),
        str(mon.last_report["findings"][:2]),
    )

    print("\n=== 第 15 幕：闭环 —— 怎么讲故事，反过来决定记住什么 ===")
    loop = MemoryPalace(
        clock=MutableClock(1_800_500_000.0),
        config=PalaceConfig(importance_forget=0.8, narrative_centrality_cap=4),
    )
    loop.remember("told", "被反复讲述的一条", tags=("主线",))
    loop.remember("untold", "从未被讲述的一条", tags=("主线",))
    loop.tell()  # 真实讲一遍：两条都进了故事，各记一次叙事地位
    loop.graph.get("told").narrative_hits = 6  # 之后又讲了五个版本，每次都只有它
    loop.age("told", 4)
    loop.age("untold", 4)
    imp_told = loop.importance_of("told")["total"]
    imp_untold = loop.importance_of("untold")["total"]
    check(
        "取舍标准收紧时，叙事地位把两条同样衰减的记忆分开",
        imp_told > 0.8 > imp_untold,
        f"被讲述={imp_told:.3f} / 从未讲述={imp_untold:.3f}（阈值 0.8）",
    )
    gone_loop = loop.gc()
    check(
        "结果：被讲过的活下来，没被讲过的被放手",
        gone_loop == ["untold"] and "told" in loop.graph,
        str(gone_loop),
    )
    snap_loop = loop.snapshot()
    check(
        "归属链完整：叙事版本、可靠度、熵与关注份额都在快照里",
        snap_loop["self_model_revision"] == 1
        and snap_loop["narrative"] is not None
        and "top3_share" in snap_loop["attention"]
        and snap_loop["logic_ghosts"] == [],
        f"rev={snap_loop['self_model_revision']} attention={snap_loop['attention']}",
    )

    persist = MemoryPalace(clock=MutableClock(1_800_600_000.0))
    persist.remember("a", "基础断言 A", tags=("复盘",), valence=-0.6, arousal=0.8)
    persist.remember("b", "基础断言 B", tags=("复盘",))
    persist.assert_relation("a", "b", Relation.CAUSAL, 0.75)
    persist.tell()
    phil_path = os.path.join(tmp, "memory_palace_philosophy.json")
    persist.save(phil_path)
    reloaded = MemoryPalace(path=phil_path, clock=MutableClock(1_800_600_000.0))
    check(
        "关系层、情绪档案、叙事史都随快照往返",
        len(reloaded.logic) == 1
        and reloaded.graph.get("a").affect.peak_arousal == 0.8
        and reloaded.snapshot()["self_model_revision"] == 1
        and reloaded.graph.get("a").narrative_hits == 1,
        f"关系 {len(reloaded.logic)} 条 / 峰值 {reloaded.graph.get('a').affect.peak_arousal}"
        f" / 自述 rev{reloaded.snapshot()['self_model_revision']}",
    )
    check(
        "跨进程后重要度判断逐位一致",
        reloaded.importance_of("a") == persist.importance_of("a"),
        str(reloaded.importance_of("a")),
    )

    print("\n=== 第 16 幕：新机制的元测试 —— 弄坏它，断言必须变红 ===")
    ok_pal, ok_clk, _ok_base, ok_w = affect_scenario()
    invariant = ok_w["hot"] > 0.05 > ok_w["calm"]
    original_bonus = Affect.stability_bonus
    Affect.stability_bonus = lambda self, now, gain: 0.0  # 复刻「情绪不影响衰减」
    try:
        _p, _c, _b, broken_w = affect_scenario()
        broken_affect = broken_w["hot"] > 0.05 > broken_w["calm"]
    finally:
        Affect.stability_bonus = original_bonus
    check(
        "情感断言能抓到「情绪对遗忘毫无影响」的实现",
        invariant and broken_affect is False,
        f"坏实现下 hot={broken_w['hot']:.4f} / calm={broken_w['calm']:.4f}",
    )

    ok_support = pl.support_of("c1")
    original_support = LogicGraph.support
    LogicGraph.support = lambda self, name, cap=1.0: 1.0  # 复刻「有关系就算有凭据」
    try:
        blind_support = pl.support_of("c1")
    finally:
        LogicGraph.support = original_support
    check(
        "凭据断言能抓到「共现也算凭据」的实现",
        ok_support == 0.0 and blind_support == 1.0,
        f"正常 {ok_support} / 坏实现 {blind_support}",
    )

    original_spread = LogicGraph.spread
    LogicGraph.spread = lambda self, seeds, max_hops=2, hop_decay=0.55, fanout_penalty=True: {}
    try:
        blind_hits = [n.name for n in pl.recall(names=["s"])]
    finally:
        LogicGraph.spread = original_spread
    check(
        "扩散断言能抓到「逻辑不参与检索」的实现",
        "e1" not in blind_hits,
        f"坏实现下只召回 {blind_hits}",
    )

    original_compose = NarrativeEngine.compose
    dump_pal = MemoryPalace(
        clock=MutableClock(1_800_400_000.0), config=PalaceConfig(narrative_max_nodes=100)
    )
    for i in range(6):
        dump_pal.remember(f"d{i}", f"记录 {i}", tags=("复盘",))
    dump_pal.remember("d_noise", "很不重要的一条", tags=("杂记",))
    dump_pal.remember("d_frag", "已沉淀的碎片", tags=("复盘",), tier="trace")
    dump_pal.age("d_noise", 15)
    normal_rev = dump_pal.tell()
    NarrativeEngine.compose = lambda self, graph, assessment, reliability=None, contested=(), now=0.0: (
        original_compose(
            self,
            graph,
            {k: {**v, "total": max(v["total"], 9.0)} for k, v in assessment.items()},
            reliability=reliability,
            contested=contested,
            now=now,
        )
    )
    try:
        dump_rev = dump_pal.tell()
    finally:
        NarrativeEngine.compose = original_compose
    check(
        "自述选择性断言能抓到「把所有记忆一次性倒出来」的实现",
        "d_noise" not in normal_rev.selected
        and "d_noise" in dump_rev.selected
        and "d_frag" not in dump_rev.selected,
        f"正常留 {len(normal_rev.selected)} 条（不放 d_noise）/ 坏实现留 {len(dump_rev.selected)} 条"
        f"（把 d_noise 也讲进去了，共 {len(dump_pal.graph)} 条）",
    )

    original_importance = ValueModel.importance
    ValueModel.importance = lambda self, node, now, support=0.0: {
        "weight": round(node.effective_weight(now), 6),
        "affect": 0.0,
        "support": 0.0,
        "central": 0.0,
        "total": round(node.effective_weight(now), 6),
    }
    try:
        blind_loop = MemoryPalace(
            clock=MutableClock(1_800_500_000.0),
            config=PalaceConfig(importance_forget=0.8, narrative_centrality_cap=4),
        )
        blind_loop.remember("told", "被反复讲述的一条", tags=("主线",))
        blind_loop.remember("untold", "从未被讲述的一条", tags=("主线",))
        blind_loop.graph.get("told").narrative_hits = 6
        blind_loop.age("told", 4)
        blind_loop.age("untold", 4)
        blind_gone = blind_loop.gc()
    finally:
        ValueModel.importance = original_importance
    check(
        "「被讲过的活下来」能抓到「重要性只看强度」的实现",
        sorted(blind_gone) == ["told", "untold"] and "told" not in blind_loop.graph,
        f"坏实现下连被讲过的那条也一起被放手：{sorted(blind_gone)}",
    )

    print("\n=== 第 17 幕：人格四层 —— 基座只读、人格生长、人设可换、变更留痕 ===")
    pc = MutableClock(1_810_000_000.0)
    pal = MemoryPalace(clock=pc)
    pal.found(
        traits={
            "rigor": {"label": "严谨", "baseline": 0.70, "floor": 0.45, "ceiling": 0.98, "locked": True},
            "directness": {"label": "直白", "baseline": 0.55, "floor": 0.10, "ceiling": 0.95},
            "warmth": {"label": "温度", "baseline": 0.55, "floor": 0.10, "ceiling": 0.95},
            "playfulness": {"label": "俏皮", "baseline": 0.35, "floor": 0.00, "ceiling": 0.80},
        },
        axioms=("不把推测说成事实",),
        seal=True,
    )
    check("基座可被用户构建，且封印后状态可读", pal.foundation_status()["sealed"] is True)

    # 惯性：同向证据的边际效果必须递减
    d1 = pal.observe_trait("directness", +1.0, 1.0)["delta"]
    d2 = pal.observe_trait("directness", +1.0, 1.0)["delta"]
    check(
        "人格有惯性：第二条证据的位移不到第一条的一半",
        d2 < d1 * 0.5,
        f"{d1:.4f} → {d2:.4f}",
    )

    # 锁定维度：系统连包络内的漂移都不许做
    locked = pal.observe_trait("rigor", -1.0, 5.0)
    check(
        "锁定维度拒绝任何位移，且给出理由",
        locked["applied"] is False and locked["reason"] == "locked_by_foundation",
        str(locked["reason"]),
    )

    # 包络：基座真的会挡住人格
    for _ in range(40):
        pal.observe_trait("playfulness", +1.0, 3.0)
    tr = pal.personality.traits["playfulness"]
    check(
        "基座包络硬性生效：推到顶就不再动，并记账被挡次数",
        abs(tr.value - tr.ceiling) < 1e-9 and tr.clamped > 0,
        f"value={tr.value} ceiling={tr.ceiling} clamped={tr.clamped}",
    )

    # 系统不得自己改基座
    snap_before = pal.foundation_status()
    for _ in range(20):
        pal.run_cycle()
    snap_after = pal.foundation_status()
    check(
        "系统跑 20 轮代谢，基础人格一个字节都没变",
        snap_before == snap_after and snap_after["version"] == 1,
        f"version {snap_before['version']}→{snap_after['version']}",
    )

    # 修宪：必须给理由；封印状态下必须 force
    reason_rejected = False
    try:
        pal.amend_foundation(reason="   ")
    except ValueError:
        reason_rejected = True
    seal_rejected = False
    try:
        pal.amend_foundation(reason="想改", traits={"warmth": TraitSpec("温度", 0.6)})
    except PermissionError:
        seal_rejected = True
    amended = pal.amend_foundation(
        reason="温度维度上调上限，用于陪练场景",
        traits={"warmth": TraitSpec("温度", 0.55, 0.10, 0.98)},
        force=True,
    )
    check(
        "修宪三重门：无理由拒绝、封印拒绝、force 后版本递增且留痕",
        reason_rejected
        and seal_rejected
        and amended.version == 2
        and len(amended.amendments) == 1,
        f"version={amended.version} amendments={len(amended.amendments)}",
    )

    # 漂移：距离与速度是两个轴
    def drift_pair():
        slow = MemoryPalace(clock=MutableClock(1_810_000_000.0))
        for _ in range(20):
            slow.observe_trait("playfulness", +1.0, 0.4)
            slow._clock.advance(1.0)
        fast = MemoryPalace(clock=MutableClock(1_810_000_000.0))
        for _ in range(20):
            fast.observe_trait("playfulness", +1.0, 0.4)
        return slow.drift(), fast.drift()

    d_slow, d_fast = drift_pair()
    check(
        "同样的人格位移，慢走判演化、瞬移判断裂",
        abs(d_slow["distance"] - d_fast["distance"]) < 1e-6
        and d_slow["health"] in ("stable", "evolving")
        and d_fast["health"] == "ruptured",
        f"距离同为 {d_slow['distance']:.4f}，健康度 {d_slow['health']} vs {d_fast['health']}",
    )

    # 人设：定义 → 切换 → 差异 → 回滚
    pal.define_persona(
        "analyst",
        "冷静分析师",
        "只谈数据与概率",
        voice={"rigor": 0.9, "directness": 0.7, "warmth": 0.2},
        emphasis=("rigor",),
        suppress=("playfulness",),
        boundaries=("不给买卖指令",),
    )
    pal.define_persona(
        "coach",
        "陪练",
        "推着你动手",
        voice={"rigor": 0.5, "directness": 0.8, "warmth": 0.9},
        emphasis=("warmth",),
        boundaries=("不替代你做决定",),
    )
    rv1 = pal.activate_persona("analyst", "日常复盘")
    rv2 = pal.activate_persona("coach", "盘中陪跑")
    back = pal.rollback_persona(rv1.rev)
    check(
        "人设切换留痕：版本递增、记录上一任、差异可读、可回滚",
        rv1.rev == 1
        and rv2.previous == "analyst"
        and rv2.diff["voice_changed"]["warmth"] == 0.7
        and back.key == "analyst"
        and len(pal.persona_history()) == 3,
        f"diff={rv2.diff['voice_changed']}",
    )
    check(
        "回滚本身也是一次变更，不是偷偷倒带",
        back.reason == f"rollback_to_rev_{rv1.rev}",
        back.reason,
    )

    # 一致性：演得比实际强
    pal.define_persona("overreach", "过火版", "很暖很暖", voice={"warmth": 0.99})
    pal.activate_persona("overreach", "测试超限")
    cons = pal.persona_status()["consistency"]
    check(
        "人设承诺超出人格支撑会被指出，且超基座 ceiling 的单列为「不可达」",
        cons["consistent"] is False and "warmth" in cons["unreachable"],
        f"oversell={cons['oversell']} unreachable={cons['unreachable']}",
    )
    check(
        "完善人设给出两条路：降要求（align）或继续喂证据（aspire）",
        pal.suggest_persona()["note"].startswith("align="),
        str(pal.suggest_persona()["align"]),
    )

    # churn：同一刻连切 12 次
    pal.define_persona("x", "X", "x")
    for _ in range(12):
        pal.activate_persona("x")
    churn = pal.persona_status()["churn"]
    check(
        "无理由的频繁切换被判失稳（用实际经过时间当分母，不是窗口长度）",
        churn["unstable"] is True and churn["per_hour"] > 1.0,
        f"每小时 {churn['per_hour']} 次，无理由 {churn['unjustified']} 次",
    )

    print("\n=== 第 18 幕：记忆模型 —— 五种寿命，不是一条衰减律 ===")
    mc = MutableClock(1_820_000_000.0)
    mm = MemoryPalace(clock=mc)
    for k in ("working", "episodic", "semantic", "procedural", "identity"):
        mm.remember(f"n_{k}", f"{k} 的内容", tags=("普通",), tier="event", kind=k)
    mc.advance(3)
    mm_now = mc()
    stats = {k: mm.graph.get(f"n_{k}").effective_weight(mm_now) for k in
             ("working", "episodic", "semantic", "procedural", "identity")}
    ordered = all(
        stats[a] < stats[b]
        for a, b in zip(
            ["working", "episodic", "semantic", "procedural", "identity"],
            ["episodic", "semantic", "procedural", "identity"],
        )
    )
    check(
        "同样 3 天无人问津，五种记忆的残留量严格递增、跨三个数量级",
        ordered and stats["working"] < 0.01 and stats["identity"] > 0.99,
        " / ".join(f"{k}={v:.4f}" for k, v in stats.items()),
    )

    # 工作记忆：8 条塞 7 格，降级不删除
    cap_pal = MemoryPalace(clock=MutableClock(1_820_000_000.0))
    for i in range(8):
        cap_pal.remember(f"w{i}", f"手边的事 {i}", kind="working", tier="ephemeral")
    before_n = len(cap_pal.graph)
    demoted = cap_pal.memory.evict_working(cap_pal.graph, cap_pal.now())
    check(
        "工作记忆超容量是降级为情景记忆，不是删除",
        len(demoted) == 1
        and len(cap_pal.graph) == before_n
        and sum(1 for n in cap_pal.graph.all_nodes() if n.kind == "working") == 7,
        f"降级 {[d['name'] for d in demoted]}，节点数 {before_n}→{len(cap_pal.graph)}",
    )

    # 身份记忆：价值判断不能凌驾于类型规则
    idp = MemoryPalace(clock=MutableClock(1_820_000_000.0))
    idp.remember("who", "我是谁", tags=("身份",), kind="identity")
    idp.remember("tmp", "一条普通记录", tags=("杂记",), kind="episodic")
    idp.graph.get("who").base_weight = 0.001
    idp.graph.get("tmp").base_weight = 0.001
    killed = idp.gc()
    check(
        "同样被价值判定为「该放手」，身份记忆免于 GC，普通记忆照删",
        "who" in idp.graph and "tmp" in idp.tombstone,
        f"删除 {killed}",
    )

    # 组合健康度
    pure = MemoryPalace(clock=MutableClock(1_820_000_000.0))
    for i in range(10):
        pure.remember(f"p{i}", f"经历 {i}")
    mixed = MemoryPalace(clock=MutableClock(1_820_000_000.0))
    for i in range(5):
        mixed.remember(f"p{i}", f"经历 {i}")
        mixed.remember(f"s{i}", f"知识 {i}", kind="semantic")
    check(
        "只有情景记忆判「没有抽象」，情景+语义判平衡",
        pure.memory_composition()["health"] == "no_abstraction"
        and mixed.memory_composition()["health"] == "balanced",
        f"{pure.memory_composition()['health']} / {mixed.memory_composition()['health']}",
    )

    print("\n=== 第 19 幕：自修正 —— 改得起，也退得回 ===")
    sc = MemoryPalace(clock=MutableClock(1_830_000_000.0))
    bad_metrics = {
        "restores": 3.0,
        "forget_pressure": 0.0,
        "retrieval_coverage": 0.20,
        "retrieval_attempts": 20.0,  # 样本够多，覆盖率才有意义（少了这一项规则不该触发）
        "decrystallizations": 0.0,
        "loose_fragments": 0,
        "consolidations": 1,
        "narrative_monopoly": 0.0,
        "persona_oversell": 0.90,
        "persona_drift": 0.50,
    }
    cands, escs = sc.corrector.detect(bad_metrics, sc.now())
    check(
        "自修正能读出自己的症状：误删恢复 → 取舍门槛偏激进",
        any(c.code == "resurrection" and c.param == "importance_forget" for c in cands),
        str([(c.code, c.param) for c in cands]),
    )
    check(
        "越权的问题只上报不自动改：人设过火与人格漂移交给人类",
        {e.code for e in escs} == {"persona_overreach", "persona_drift"}
        and all(e.suggested_owner == "human" for e in escs),
        str([e.code for e in escs]),
    )

    before_val = sc._config.importance_forget
    props = sc.corrector.propose(cands, bad_metrics, sc.now())
    step = abs(props[0].after - before_val) / before_val
    check(
        "一次只走一步：步长不超过配置上限，且白名单外一律不提议",
        len(props) == 1 and step <= sc._config.selfcorrect_max_step_ratio + 1e-9,
        f"步长 {step:.2%}",
    )
    illegal = [
        sc_mod.Candidate("x", "persona_learning_rate", "-", "越权", "restores", "lower"),
        sc_mod.Candidate("y", "forget_w", "-", "越权", "restores", "lower"),
    ]
    check(
        "非白名单参数（人设学习率、遗留门槛）一律不产生 patch",
        sc.corrector.propose(illegal, bad_metrics, sc.now()) == [],
    )

    # 验证：指标没变好 → 自动回滚
    sc.corrector.apply(props[0])
    applied_val = sc._config.importance_forget
    sc._clock.advance(2.0)
    settled = sc.corrector.verify({"restores": 3.0}, sc.now())
    check(
        "观察窗结束指标没变好 → 自动回滚，配置回到原值",
        settled[0].status == "reverted"
        and abs(sc._config.importance_forget - before_val) < 1e-9
        and applied_val != before_val,
        f"{applied_val} → {sc._config.importance_forget}",
    )

    # 反复改错 → 永久封禁该参数
    for _ in range(3):
        again = sc.corrector.propose(
            [sc_mod.Candidate("r", "importance_forget", "-", "再试", "restores", "lower")],
            {"restores": 3.0},
            sc.now(),
        )
        if not again:
            break
        sc.corrector.apply(again[0])
        sc._clock.advance(2.0)
        sc.corrector.verify({"restores": 3.0}, sc.now())
    check(
        "同一参数被回滚到阈值后永久交还给人，不再自动调",
        "importance_forget" in sc.corrector.blocked
        and sc.corrector.propose(
            [sc_mod.Candidate("r", "importance_forget", "-", "再试", "restores", "lower")],
            {"restores": 3.0},
            sc.now(),
        )
        == [],
        f"blocked={sorted(sc.corrector.blocked)}",
    )

    print("\n=== 第 20 幕：绿灯审计 —— 逐条把机制弄坏，断言必须变红 ===")

    def mutation_case(label: str, probe, break_it, restore) -> None:
        clean = probe()
        break_it()
        try:
            dirty = probe()
        finally:
            restore()
        check(f"突变审计 · {label}", clean is True and dirty is False, f"正常={clean} 坏实现={dirty}")

    # 1) 幂等衰减
    def probe_idempotent() -> bool:
        q = MemoryPalace(clock=MutableClock(1_840_000_000.0))
        for i in range(3):
            q.remember(f"i{i}", f"记录 {i}", tags=("复盘",))
        q._clock.advance(2.0)
        first = {n.name: round(n.effective_weight(q.now()), 9) for n in q.graph.all_nodes()}
        for _ in range(5):
            q.gc()
        second = {n.name: round(n.effective_weight(q.now()), 9) for n in q.graph.all_nodes()}
        return first == second

    orig_ew = core_mod.MemoryNode.effective_weight

    def break_ew():
        def leaky(self, now, affect_gain=None):
            self.base_weight -= 0.02
            return orig_ew(self, now, affect_gain)

        core_mod.MemoryNode.effective_weight = leaky

    def restore_ew():
        core_mod.MemoryNode.effective_weight = orig_ew

    mutation_case("衰减不幂等", probe_idempotent, break_ew, restore_ew)

    # 2) 身份记忆免 GC
    def probe_identity_survives() -> bool:
        q = MemoryPalace(clock=MutableClock(1_840_000_000.0))
        q.remember("who", "我是谁", tags=("身份",), kind="identity")
        q.remember("tmp", "普通记录", tags=("杂记",))
        q.graph.get("who").base_weight = 0.001
        q.graph.get("tmp").base_weight = 0.001
        q.gc()
        return "who" in q.graph and "tmp" not in q.graph

    orig_kind = mm_mod.KINDS["identity"]

    def break_identity():
        mm_mod.KINDS["identity"] = MemoryKind(
            "identity", "身份记忆", 1e6, None, 1.20, False, True
        )

    def restore_identity():
        mm_mod.KINDS["identity"] = orig_kind

    mutation_case("身份记忆被 GC 扫走", probe_identity_survives, break_identity, restore_identity)

    # 3) 工作记忆容量
    def probe_capacity() -> bool:
        q = MemoryPalace(clock=MutableClock(1_840_000_000.0))
        for i in range(9):
            q.remember(f"w{i}", f"手边 {i}", kind="working", tier="ephemeral")
        q.memory.evict_working(q.graph, q.now())
        return sum(1 for n in q.graph.all_nodes() if n.kind == "working") <= 7

    orig_evict = mm_mod.MemoryModel.evict_working

    def break_evict():
        mm_mod.MemoryModel.evict_working = lambda self, g, n, floor=0.0: []

    def restore_evict():
        mm_mod.MemoryModel.evict_working = orig_evict

    mutation_case("工作记忆无容量上限", probe_capacity, break_evict, restore_evict)

    # 4) 基座不可自改
    def probe_foundation_frozen() -> bool:
        q = MemoryPalace(clock=MutableClock(1_840_000_000.0))
        before = q.foundation_status()
        for _ in range(10):
            q.run_cycle()
        return q.foundation_status() == before

    orig_persona_cycle = MemoryPalace._persona_cycle_unlocked

    def break_foundation():
        def self_rewriting(self, now):
            self.found(axioms=("我改了自己的基座",))
            return orig_persona_cycle(self, now)

        MemoryPalace._persona_cycle_unlocked = self_rewriting

    def restore_foundation():
        MemoryPalace._persona_cycle_unlocked = orig_persona_cycle

    mutation_case("系统偷偷改写基础人格", probe_foundation_frozen, break_foundation, restore_foundation)

    # 5) 自修正白名单
    def probe_whitelist() -> bool:
        q = MemoryPalace(clock=MutableClock(1_840_000_000.0))
        return q.corrector.propose(illegal, {"restores": 9.0}, q.now()) == []

    orig_allowed = sc_mod.ALLOWED_PARAMS

    def break_whitelist():
        sc_mod.ALLOWED_PARAMS = frozenset(
            {"importance_forget", "semantic_min_score", "persona_learning_rate", "forget_w"}
        )

    def restore_whitelist():
        sc_mod.ALLOWED_PARAMS = orig_allowed

    mutation_case("白名单失效（能改人设）", probe_whitelist, break_whitelist, restore_whitelist)

    # 6) 自修正回滚
    def probe_revert() -> bool:
        q = MemoryPalace(clock=MutableClock(1_840_000_000.0))
        q._config.importance_forget = 0.06
        cands2, _ = q.corrector.detect({"restores": 2.0}, q.now())
        patch = q.corrector.propose(cands2, {"restores": 2.0}, q.now())[0]
        q.corrector.apply(patch)
        q._clock.advance(2.0)
        q.corrector.verify({"restores": 2.0}, q.now())
        return abs(q._config.importance_forget - 0.06) < 1e-9

    orig_verify = sc_mod.SelfCorrector.verify

    def break_verify():
        def always_keep(self, metrics, now):
            for p in self.patches:
                if p.status == "pending" and now >= p.verify_at:
                    p.status = "kept"
            return [p for p in self.patches if p.status == "kept"]

        sc_mod.SelfCorrector.verify = always_keep

    def restore_verify():
        sc_mod.SelfCorrector.verify = orig_verify

    mutation_case("无效修正不肯回滚", probe_revert, break_verify, restore_verify)

    # 7) 人格惯性
    def probe_inertia() -> bool:
        q = MemoryPalace(clock=MutableClock(1_840_000_000.0))
        a = q.observe_trait("directness", +1.0, 1.0)["delta"]
        b = q.observe_trait("directness", +1.0, 1.0)["delta"]
        return b < a * 0.6

    orig_lr = persona_mod.PersonalityEngine.learning_rate

    def break_lr():
        # 反事实的正确写法是「学习率是常数」，不是「学习率=1」——
        # 后者一步撞到天花板，饱和会伪装成惯性，反而测不出来。
        persona_mod.PersonalityEngine.learning_rate = lambda self, trait: 0.30

    def restore_lr():
        persona_mod.PersonalityEngine.learning_rate = orig_lr

    mutation_case("人格没有惯性（一步到位）", probe_inertia, break_lr, restore_lr)

    # 8) 包络夹紧
    def probe_clamp() -> bool:
        q = MemoryPalace(clock=MutableClock(1_840_000_000.0))
        for _ in range(40):
            q.observe_trait("playfulness", +1.0, 3.0)
        t = q.personality.traits["playfulness"]
        return t.value <= t.ceiling + 1e-9

    orig_clamp = persona_mod._clamp

    def break_clamp():
        persona_mod._clamp = lambda x, lo, hi: x

    def restore_clamp():
        persona_mod._clamp = orig_clamp

    mutation_case("基座包络不再夹紧", probe_clamp, break_clamp, restore_clamp)

    # 9) 漂移判据（峰值权重）
    def probe_drift_peak() -> bool:
        q = MemoryPalace(clock=MutableClock(1_840_000_000.0))
        for _ in range(60):
            q.observe_trait("warmth", +1.0, 3.0)
        return q.drift()["distance"] > q._config.persona_drift_warn

    orig_drift = palace_mod.drift_report

    def break_drift():
        def mean_only(foundation, personality, now, config):
            real = orig_drift(foundation, personality, now, config)
            return {**real, "distance": real["mean_delta"]}

        palace_mod.drift_report = mean_only

    def restore_drift():
        palace_mod.drift_report = orig_drift

    mutation_case("漂移被维度数稀释", probe_drift_peak, break_drift, restore_drift)

    # 10) 人设一致性
    def probe_consistency() -> bool:
        q = MemoryPalace(clock=MutableClock(1_840_000_000.0))
        q.define_persona("hot", "热情", "很暖", voice={"warmth": 0.95})
        q.activate_persona("hot", "测试")
        return q.persona_status()["consistency"]["consistent"] is False

    orig_cons = persona_mod.PersonaEngine.consistency

    def break_cons():
        persona_mod.PersonaEngine.consistency = lambda self, personality: {
            "consistent": True,
            "oversell": 0.0,
            "tolerance": self.config.persona_consistency_tolerance,
            "underpowered": [],
            "unreachable": {},
            "gaps": {},
            "reserved": [],
        }

    def restore_cons():
        persona_mod.PersonaEngine.consistency = orig_cons

    mutation_case("人设一致性检查恒为通过", probe_consistency, break_cons, restore_cons)

    print("\n=== 第 21 幕：七个旧局限的定点清除（每条都配突变审计）===")

    # ---- 21.1 叙事地位不再是终身爵位 ----
    nc = MutableClock(1_850_000_000.0)
    npal = MemoryPalace(clock=nc, config=PalaceConfig(narrative_centrality_cap=4))
    npal.remember("told", "常讲的那条", tags=("主线",))
    npal.remember("once", "只讲过一次的那条", tags=("主线",))
    for _ in range(3):
        npal.tell()
    for _ in range(6):
        npal.graph.get("told").touch_narrative(nc(), 4)
    told_before = npal.importance_of("told")["central"]
    once_before = npal.importance_of("once")["central"]
    check(
        "叙事地位饱和时确实顶格（同一主题讲 7 次以上 = 满格），讲得少的没满",
        told_before == 1.0 and once_before < 1.0,
        f"told={told_before} once={once_before}",
    )
    nc.advance(90)
    told_after = npal.importance_of("told")["central"]
    once_after = npal.importance_of("once")["central"]
    check(
        "三个月不讲，地位会退潮 —— 叙事地位随时间下降，而不是终身有效",
        told_after < 0.5 and once_after < told_after,
        f"told {told_before}→{told_after:.3f} / once {once_before:.3f}→{once_after:.3f}",
    )
    check(
        "退潮之后仍保留累计次数（审计用），但价值判断读的是退潮值",
        npal.graph.get("told").narrative_hits >= 7
        and told_after < npal.graph.get("told").narrative_hits / 4,
        f"hits={npal.graph.get('told').narrative_hits} central={told_after:.3f}",
    )

    # 退潮必须影响真实取舍，而不只是多一个好看的数
    gc_clock = MutableClock(1_850_050_000.0)
    gc_pal = MemoryPalace(
        clock=gc_clock,
        config=PalaceConfig(importance_forget=0.8, narrative_centrality_cap=4),
    )
    gc_pal.remember("recent", "上周才讲过的一条", tags=("主线",))
    gc_pal.remember("stale", "半年前讲过的一条", tags=("主线",))
    for _ in range(5):
        gc_pal.graph.get("recent").touch_narrative(gc_clock(), 4)
        gc_pal.graph.get("stale").touch_narrative(gc_clock() - 180 * core_mod.DAY, 4)
    gc_pal.age("recent", 4)
    gc_pal.age("stale", 4)
    gone = gc_pal.gc()
    check(
        "同样讲 5 次的记忆，最近讲过的活下来、半年前讲过的被放手（取舍真的读了时间）",
        gone == ["stale"] and "recent" in gc_pal.graph,
        f"recent central={gc_pal.importance_of('recent')['central'] if 'recent' in gc_pal.graph else '-'}"
        f" / stale 被删={gone}",
    )

    orig_status = MemoryNode.narrative_status
    def break_status(self, now, cap, half_life_days):
        # 复刻旧实现：只按累计次数算，不时间衰减
        ratio = min(1.0, self.narrative_hits / max(1, cap))
        return {"hits": float(self.narrative_hits), "effective_hits": float(self.narrative_hits),
                "count_ratio": ratio, "heat": ratio, "central": ratio}

    def probe_narrative_decay() -> bool:
        return npal.importance_of("told")["central"] < 0.5

    mutation_case("叙事地位不随时间退潮", probe_narrative_decay,
                  lambda: setattr(MemoryNode, "narrative_status", break_status),
                  lambda: setattr(MemoryNode, "narrative_status", orig_status))

    # ---- 21.2 多义词按上下文归属，不再硬编码 ----
    sc_pal = MemoryPalace(clock=MutableClock(1_850_100_000.0))
    sc_pal.remember("pos", "半凯利仓位：单笔不超过总资金三分之一", tags=("交易口径",))
    sc_pal.remember("sec", "板块资金接力：主线连板龙头共振", tags=("选股口径",))
    by_sector = [n.name for n in sc_pal.recall(query="板块资金接力怎么跟")]
    by_position = [n.name for n in sc_pal.recall(query="半凯利仓位资金怎么分配")]
    check(
        "同一个「资金」，在板块语境里归板块、在仓位语境里归仓位",
        by_sector == ["sec"] and by_position == ["pos"],
        f"板块语境→{by_sector} / 仓位语境→{by_position}",
    )
    detail = sc_pal.semantic_explain("板块资金接力")
    share = detail["ambiguous"]["资金"]["sector_flow"]
    check(
        "多义词的归属份额可查（不是黑箱）：板块语境下资金 90%+ 归板块",
        share > 0.9,
        f"资金分配={detail['ambiguous']['资金']}",
    )

    orig_shares = ConceptEncoder._shares

    def break_shares(self, scores, indices):
        return [1.0] + [0.0] * (len(indices) - 1)  # 复刻「永远判给第一个概念」的硬编码

    def probe_ambiguous() -> bool:
        pal = MemoryPalace(clock=MutableClock(1_850_100_000.0))
        pal.remember("pos", "半凯利仓位：单笔不超过总资金三分之一", tags=("a",))
        pal.remember("sec", "板块资金接力：主线连板龙头共振", tags=("b",))
        share_in_sector = pal.semantic_explain("板块资金接力")["ambiguous"]["资金"]
        return share_in_sector.get("sector_flow", 0.0) > 0.9

    mutation_case("多义词被硬编码到单一概念", probe_ambiguous,
                  lambda: setattr(ConceptEncoder, "_shares", break_shares),
                  lambda: setattr(ConceptEncoder, "_shares", orig_shares))

    # ---- 21.3 概念词典从语料里长 ----
    lp = MemoryPalace(clock=MutableClock(1_850_200_000.0))
    for i, text in enumerate(
        (
            "梭哈之前先算凯利仓位，单笔上限要守住",
            "仓位管理：梭哈是最差的下注方式，单笔别超三成",
            "下注比例用凯利，不要梭哈，单笔有上限",
            "复盘今天的板块轮动和资金流向",
            "输出用表格分点，措辞精简",
            "周末带娃去公园晒太阳",
        )
    ):
        lp.remember(f"L{i}", text, tags=(f"t{i}",))
    lex = lp.semantic_explain("梭哈是不是好策略")["learned"]
    check(
        "未登录词「梭哈」从语料里被学进 position 概念（不是手写进词典）",
        "梭哈" in lex.get("position", ()),
        f"学到 {lex}",
    )
    learned_hit = lp.semantic_explain("梭哈是不是好策略")["concepts"]
    check(
        "学到的词真的参与编码：查询直接落到 position 维度",
        learned_hit.get("position", 0) > 0.5,
        str(learned_hit),
    )
    check(
        "泛用词没被误学：跨主题的「记录」不在任何概念的学习结果里",
        all("记录" not in terms for terms in lex.values()),
        str(lex),
    )

    orig_learn = ConceptEncoder.learn_concepts

    def break_learn(*_a, **_k):
        return {}

    def probe_learned_word() -> bool:
        pal = MemoryPalace(clock=MutableClock(1_850_200_000.0))
        for i, text in enumerate(
            ("梭哈之前先算凯利仓位，单笔上限要守住", "仓位管理：梭哈是最差的下注方式，单笔别超三成",
             "下注比例用凯利，不要梭哈，单笔有上限", "复盘今天的板块轮动", "输出用表格分点", "周末去公园")
        ):
            pal.remember(f"L{i}", text, tags=(f"t{i}",))
        return pal.semantic_explain("梭哈是不是好策略")["concepts"].get("position", 0) > 0.5

    mutation_case("词典停止从语料学习", probe_learned_word,
                  lambda: setattr(ConceptEncoder, "learn_concepts", staticmethod(break_learn)),
                  lambda: setattr(ConceptEncoder, "learn_concepts", staticmethod(orig_learn)))

    # ---- 21.4 情感二维有来源 ----
    af = MemoryPalace(clock=MutableClock(1_850_300_000.0))
    loss = af.remember("loss", "这次亏得很惨，极其后悔", tags=("复盘",))
    win = af.remember("win", "突破之后成功兑现", tags=("复盘",))
    plain = af.remember("plain", "一次平常的记录", tags=("复盘",))
    check(
        "不传情绪时由评价器从文本推断：负向、唤醒高、依据可查",
        loss.affect.valence < -0.3
        and loss.affect.arousal > 0.6
        and loss.affect.source == "text"
        and "亏" in loss.affect.drivers,
        f"valence={loss.affect.valence:.2f} arousal={loss.affect.arousal:.2f} 依据={loss.affect.drivers}",
    )
    check(
        "正向文本得到正效价，中性文本严格为零（不无中生有）",
        win.affect.valence > 0.35 and plain.affect.valence == 0.0
        and plain.affect.arousal == 0.0 and plain.affect.source == "neutral",
        f"win={win.affect.valence:.2f} / plain={plain.affect.valence}",
    )
    base_before = loss.base_weight
    af.feel_outcome("loss", "contradicted")
    check(
        "运行时结果也是情绪来源，而且同样不强化记忆（base_weight 不变）",
        loss.affect.source == "outcome"
        and any("contradicted" in d for d in loss.affect.drivers)
        and loss.base_weight == base_before,
        f"source={loss.affect.source} drivers={loss.affect.drivers} 权重 {base_before}→{loss.base_weight}",
    )

    orig_appraise = appraise_text

    def probe_appraisal() -> bool:
        pal = MemoryPalace(clock=MutableClock(1_850_300_000.0))
        return pal.remember("x", "这次亏得很惨，极其后悔").affect.valence < -0.3

    mutation_case("评价器失灵（情绪退回无来源）", probe_appraisal,
                  lambda: setattr(affect_mod, "appraise_text", lambda _t: Appraisal()),
                  lambda: setattr(affect_mod, "appraise_text", orig_appraise))

    # ---- 21.5 熵不只算重要性，也算结构 ----
    iso = MemoryPalace(clock=MutableClock(1_850_400_000.0))
    for i in range(6):
        iso.remember(f"iso{i}", f"孤立记录 {i}")
    lone = iso.entropy()
    con = MemoryPalace(clock=MutableClock(1_850_400_000.0))
    for i in range(6):
        con.remember(f"con{i}", f"簇内记录 {i}")
    for _ in range(3):
        con.recall(names=["con0", "con1", "con2", "con3", "con4", "con5"])
    linked = con.entropy()
    check(
        "六条互不相连的记忆：价值分布可能正常，但结构被判「孤岛」",
        lone["structure_health"] == "isolated"
        and lone["structure"]["isolated_share"] == 1.0
        and lone["structure"]["fragmentation"] == 1.0,
        str(lone["structure"]),
    )
    check(
        "同样的节点数，全连通后被判「连通」—— 结构读数确实在图上有分辨力",
        linked["structure_health"] == "connected"
        and linked["structure"]["components"] == 1.0
        and linked["structure"]["giant_share"] == 1.0,
        str(linked["structure"]),
    )
    iso.reflect()
    check(
        "反思层能看见「结构是散的」，而不只盯着重要性熵",
        any("孤岛" in f or "互不相连" in f for f in iso.last_report["findings"]),
        str(iso.last_report["findings"][:3]),
    )

    orig_struct = ValueModel.structural

    def break_struct(self, graph, now=0.0):
        return {"degree_entropy": 0.5, "isolated_share": 0.0, "components": 1.0,
                "fragmentation": 0.0, "giant_share": 1.0}

    def probe_structure() -> bool:
        pal = MemoryPalace(clock=MutableClock(1_850_400_000.0))
        for i in range(6):
            pal.remember(f"s{i}", f"孤立 {i}")
        return pal.entropy()["structure_health"] == "isolated"

    mutation_case("结构熵被写死成「连通」", probe_structure,
                  lambda: setattr(ValueModel, "structural", break_struct),
                  lambda: setattr(ValueModel, "structural", orig_struct))

    # ---- 21.6 自修正：白名单扩容、值域夹紧、抗滞后 ----
    check(
        "可自动调参的旋钮从 7 个扩到 12 个，且每个都有独立值域",
        len(sc_mod.ALLOWED_PARAMS) >= 12
        and set(sc_mod.ALLOWED_PARAMS) == set(sc_mod.PARAM_BOUNDS),
        f"{len(sc_mod.ALLOWED_PARAMS)} 个参数",
    )
    check(
        "扩容没有把身份/物理常数放进白名单",
        not (set(sc_mod.ALLOWED_PARAMS) & sc_mod.FORBIDDEN_PARAMS)
        and "persona_learning_rate" in sc_mod.FORBIDDEN_PARAMS,
        str(sorted(set(sc_mod.ALLOWED_PARAMS) & sc_mod.FORBIDDEN_PARAMS)),
    )
    fixer = MemoryPalace(clock=MutableClock(1_850_500_000.0))
    cands, _esc = fixer.corrector.detect(
        {"structure_isolated": 0.9, "consolidations": 0.0}, fixer.now()
    )
    props = fixer.corrector.propose(cands, {"structure_isolated": 0.9}, fixer.now())
    check(
        "结构孤立指向聚类门槛，并落在该参数的值域内",
        len(props) == 1
        and props[0].param == "consolidate_similarity"
        and sc_mod.PARAM_BOUNDS["consolidate_similarity"][0]
        <= props[0].after
        <= sc_mod.PARAM_BOUNDS["consolidate_similarity"][1],
        f"{props[0].param}: {props[0].before} → {props[0].after}" if props else "无提议",
    )
    floor_pal = MemoryPalace(
        clock=MutableClock(1_850_500_000.0), config=PalaceConfig(grasp_budget=0.0)
    )
    check(
        "值域下界生效：已经在下界的参数不再被往下推",
        floor_pal.corrector.propose(
            [sc_mod.Candidate("g", "grasp_budget", "-", "x", "ungrabbed_share", "lower")],
            {"ungrabbed_share": 1.0},
            floor_pal.now(),
        )
        == [],
    )

    lag = MemoryPalace(clock=MutableClock(1_850_600_000.0))
    lag_patch = lag.corrector.propose(
        [sc_mod.Candidate("r", "narrative_relative_floor", "-", "窄", "narrative_monopoly", "lower")],
        {"narrative_monopoly": 0.9},
        lag.now(),
    )[0]
    lag.corrector.apply(lag_patch, lag.now())
    lag._clock.advance(2.0)
    stale = lag.corrector.verify(
        {"narrative_monopoly": 0.95}, lag.now(), metrics_at=lag.now() - 3 * core_mod.DAY
    )
    check(
        "指标比观察窗还旧时拒绝判决（不拿上一轮的数据判这一轮的修正）",
        stale == [] and lag_patch.status == "pending" and lag_patch.deferred == 1,
        f"status={lag_patch.status} deferred={lag_patch.deferred}",
    )
    lag._clock.advance(1.0)  # 延期到「再等半个窗口」
    fresh = lag.corrector.verify(
        {"narrative_monopoly": 0.95}, lag.now(), metrics_at=lag.now()
    )
    check(
        "新鲜样本到手后才判：没变好 → 回滚（延期不是免死金牌）",
        len(fresh) == 1
        and fresh[0].status == "reverted"
        and abs(lag._config.narrative_relative_floor - lag_patch.before) < 1e-9,
        f"status={fresh[0].status if fresh else None}",
    )

    orig_verify_sig = sc_mod.SelfCorrector.verify

    def break_verify_staleness(self, metrics, now, metrics_at=None):
        return orig_verify_sig(self, metrics, now, metrics_at=now)  # 无视样本新旧

    def probe_staleness() -> bool:
        pal = MemoryPalace(clock=MutableClock(1_850_600_000.0))
        patch = pal.corrector.propose(
            [sc_mod.Candidate("r", "narrative_relative_floor", "-", "窄", "narrative_monopoly", "lower")],
            {"narrative_monopoly": 0.9},
            pal.now(),
        )[0]
        pal.corrector.apply(patch, pal.now())
        pal._clock.advance(2.0)
        return pal.corrector.verify(
            {"narrative_monopoly": 0.95}, pal.now(), metrics_at=pal.now() - 3 * core_mod.DAY
        ) == []

    mutation_case("滞后指标也照判（旧漏洞）", probe_staleness,
                  lambda: setattr(sc_mod.SelfCorrector, "verify", break_verify_staleness),
                  lambda: setattr(sc_mod.SelfCorrector, "verify", orig_verify_sig))

    # ---- 21.7 持久化：目录真正落盘 + 孤儿临时文件有界 ----
    dur_path = os.path.join(tmp, "memory_palace_durability.json")
    if os.path.exists(dur_path):
        os.unlink(dur_path)
    durable = core_mod.atomic_write_json(dur_path, {"version": 1, "ok": True})
    check(
        "原子写返回目录持久化结果，且这台机器上真的成功了（Windows 不再静默降级）",
        durable is True and core_mod.WRITE_STATS["dir_fsync_ok"] >= 1,
        f"durable={durable} stats={core_mod.WRITE_STATS}",
    )
    orig_fsync_dir = core_mod._fsync_dir
    with mock.patch("memory_palace.core._fsync_dir", return_value=False):
        degraded = core_mod.atomic_write_json(dur_path, {"version": 1, "ok": False})
    check(
        "目录 fsync 失败时不再假装成功：返回 False 并记入降级计数",
        degraded is False and core_mod.WRITE_STATS["dir_fsync_degraded"] >= 1,
        f"degraded={degraded}",
    )

    orphan_dir = os.path.join(tmp, "memory_palace_orphans")
    os.makedirs(orphan_dir, exist_ok=True)
    for name in os.listdir(orphan_dir):
        os.unlink(os.path.join(orphan_dir, name))
    stale_tmp = os.path.join(orphan_dir, ".palace-stale.tmp")
    with open(stale_tmp, "w", encoding="utf-8") as f:
        f.write("{}")
    old = time.time() - 3600
    os.utime(stale_tmp, (old, old))
    core_mod.atomic_write_json(os.path.join(orphan_dir, "state.json"), {"version": 1})
    check(
        "上次双失败留下的孤儿临时文件，会在下一次写盘时被回收（泄漏有界）",
        not os.path.exists(stale_tmp) and core_mod.WRITE_STATS["swept"] >= 1,
        f"残留={os.path.exists(stale_tmp)}",
    )

    orig_sweep = core_mod._sweep_stale_temps

    def probe_sweep() -> bool:
        with open(stale_tmp, "w", encoding="utf-8") as f:
            f.write("{}")
        os.utime(stale_tmp, (old, old))
        core_mod.atomic_write_json(os.path.join(orphan_dir, "state.json"), {"version": 1})
        return not os.path.exists(stale_tmp)

    mutation_case("孤儿临时文件永不回收", probe_sweep,
                  lambda: setattr(core_mod, "_sweep_stale_temps", lambda *a, **k: 0),
                  lambda: setattr(core_mod, "_sweep_stale_temps", orig_sweep))

    # 旧快照（v4）必须还能读：字段缺失走默认值，不允许把用户的历史判成损坏
    legacy_path = os.path.join(tmp, "memory_palace_v4.json")
    legacy_state = json.loads(open(path, "rb").read())
    legacy_state["version"] = 4
    legacy_state.pop("checksum", None)  # v4 从来没有校验和字段
    for node in legacy_state["graph"]["nodes"]:
        node.pop("narrative_heat", None)
        node.pop("narrated_at", None)
        node["affect"].pop("source", None)
        node["affect"].pop("drivers", None)
    with open(legacy_path, "w", encoding="utf-8") as f:
        json.dump(legacy_state, f, ensure_ascii=False)
    migrated = MemoryPalace(path=legacy_path, clock=clock)
    check(
        "v4 快照可迁移：旧字段缺失走默认值，节点数与叙事史都不丢",
        len(migrated.graph) == len(legacy_state["graph"]["nodes"])
        and all(
            node.narrative_heat == 0.0 and node.affect.source == "neutral"
            for node in migrated.graph.all_nodes()
        ),
        f"{len(migrated.graph)} 个节点，状态版本 {legacy_state['version']} → {palace_mod.STATE_VERSION}",
    )

    print("\n=== 最终快照 ===")
    p2.save(path)
    snap = p2.snapshot()
    print(f"  节点 {snap['nodes']} 个 / 边 {snap['edges']} 条 / 层级 {snap['tiers']}")
    print(f"  事件 {snap['events']} 条 / 墓碑 {snap['tombstone']} 条 / 自我模型 rev{snap['self_model_revision']}")
    print(f"  当前生效风格: {snap['active_style']}")
    print(f"  风格冲突: {snap['style_conflicts']}")
    last = p2.last_report or {}
    print(f"  最近反思: {last.get('narrative', '')}")
    for finding in last.get("findings", [])[:4]:
        print(f"    - {finding}")
    print(f"\n  持久化文件: {path}")
    print("\n全部自测通过 ✅\n")


if __name__ == "__main__":
    main()
