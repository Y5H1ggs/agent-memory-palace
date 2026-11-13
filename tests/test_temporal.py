"""test_temporal.py - 双时态、写入去重、弃答的断言（含反事实验证）。

    python tests/test_temporal.py
    pytest -q

这一组测的都不是「功能有没有」，而是**语义对不对**，因为这三块最容易写出
「看起来在跑、其实在骗人」的实现：

  1. 双时态：`supersede` 必须让旧事实**到期**而不是消失，且 as_of 能回看。
     如果只删不标，as_of 就查不到；如果只标不滤，检索会把两句一起端给模型。
  2. 写入去重：只有「几乎一字不差」才合并。语义相近但补充了新信息的**必须**新建，
     否则合并会静默吃掉新内容 —— 这是最危险的失败方式，因为它不报错。
  3. 弃答：无关问题必须明确说「没有相关记忆」，而不是拿最重的两条凑数；
     但钉死的硬规则**仍然要注入** —— 规则与问题无关也要遵守，这不叫猜。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory_palace import MemoryPalace, MutableClock, PalaceConfig
from memory_palace.cli import build_context, format_context
from memory_palace.core import MemoryNode

DAY = 86400.0
PASS = 0
FAIL = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {label}" + (f"  · {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {label}" + (f"  · {detail}" if detail else ""))


def fresh(start: float = 1_980_000_000.0) -> tuple[MemoryPalace, MutableClock]:
    clock = MutableClock(start)
    return MemoryPalace(clock=clock), clock


# ---------------- 1. 双时态 ----------------

def test_bitemporal() -> None:
    print("\n双时态：事实有有效期")
    palace, clock = fresh()
    palace.remember("pos-v1", "单笔不超过总资金三分之一", tags=("仓位",), tier="fact")
    clock.advance(10)
    palace.remember(
        "pos-v2", "单笔不超过总资金五分之一", tags=("仓位",), tier="fact",
        pinned=True,  # 钉死：这一组只测「历史到期」，不掺价值遗忘
        supersedes=["pos-v1"],
    )
    old = palace.graph.get("pos-v1")

    check(
        "旧事实没有被删除，而是到期（valid_to 有值 + 记下取代者）",
        old is not None and old.valid_to is not None and old.superseded_by == "pos-v2",
        f"valid_to={old.valid_to if old else None} by={old.superseded_by if old else None}",
    )
    check(
        "取代关系双向可查（新节点记着自己取代了谁）",
        palace.graph.get("pos-v2").supersedes == ("pos-v1",),
        str(palace.graph.get("pos-v2").supersedes),
    )
    check(
        "节点数不因取代而减少（历史仍在图里，可回溯）",
        len(palace.graph) == 2,
        f"nodes={len(palace.graph)}",
    )
    check(
        "默认检索只给新事实，不把两句一起端上去",
        [n.name for n in palace.recall(query="单笔上限")] == ["pos-v2"],
        str([n.name for n in palace.recall(query="单笔上限")]),
    )
    check(
        "as_of 回看十天前，得到的是旧事实",
        [n.name for n in palace.recall(query="单笔上限", as_of=clock() - 5 * DAY)] == ["pos-v1"],
        str([n.name for n in palace.recall(query="单笔上限", as_of=clock() - 5 * DAY)]),
    )
    check(
        "as_of 早于旧事实的生效时刻 → 空（那时它还不存在）",
        palace.recall(query="单笔上限", as_of=clock() - 30 * DAY) == [],
        str([n.name for n in palace.recall(query="单笔上限", as_of=clock() - 30 * DAY)]),
    )
    check(
        "失效的事实不进重要性账本（不污染熵与注入排序）",
        "pos-v1" not in palace.assessment(),
        str(sorted(palace.assessment())),
    )
    check(
        "失效的事实不进结构读数（连通分量只数有效节点，否则会凭空多一块孤岛）",
        palace.value.structural(palace.graph)["components"] == 1.0,
        str(palace.value.structural(palace.graph)),
    )

    # 历史保留期：到期后回收，但进墓碑而不是凭空消失
    clock.advance(palace._config.history_retention_days + 1)
    deleted = palace.gc()
    check(
        "超过历史保留期才回收，且进墓碑（仍可 restore）",
        "pos-v1" in deleted and "pos-v1" in palace.tombstone,
        f"deleted={deleted} / tomb={list(palace.tombstone)}",
    )
    check(
        "新事实不受影响",
        palace.graph.get("pos-v2") is not None and not palace.graph.get("pos-v2").is_retired(),
        str(palace.graph.get("pos-v2") is not None),
    )


def test_bitemporal_counterfactual() -> None:
    """反事实：把 is_valid_at 改成恒真，上面的过滤断言必须变红。"""
    print("\n双时态 · 反事实（证明断言真的在测这条逻辑）")
    palace, clock = fresh()
    palace.remember("a", "旧结论：单笔三分之一", tags=("仓位",), tier="fact")
    clock.advance(5)
    palace.remember("b", "新结论：单笔五分之一", tags=("仓位",), tier="fact", supersedes=["a"])

    original = MemoryNode.is_valid_at
    try:
        MemoryNode.is_valid_at = lambda self, moment: True  # 故意弄坏
        broken = [n.name for n in palace.recall(query="单笔")] == ["b"]
    finally:
        MemoryNode.is_valid_at = original
    check(
        "去掉有效期判定后，检索会把旧事实也返回（断言确实依赖它）",
        broken is False,
        f"被弄坏后「只返回新事实」仍成立={broken}",
    )


# ---------------- 2. 写入去重 ----------------

def test_dedupe() -> None:
    print("\n写入去重：只抓「同一句话说两遍」")
    palace, _ = fresh()
    palace.remember("k1", "单笔不超过总资金三分之一", tags=("仓位",), tier="fact", merge=True)
    before = len(palace.graph)
    twin = palace.remember(
        "k2", "单笔不超过总资金三分之一", tags=("纪律",), tier="fact", merge=True
    )
    check(
        "几乎一字不差 → 不新建节点，返回已有那条",
        twin.name == "k1" and len(palace.graph) == before,
        f"返回={twin.name} / nodes={before}→{len(palace.graph)}",
    )
    check(
        "合并会强化已有条目（想起次数增加）",
        twin.access_count >= 2,
        f"access_count={twin.access_count}",
    )
    check(
        "合并会把新标签并进去（信息不丢）",
        set(twin.tags) == {"仓位", "纪律"},
        str(sorted(twin.tags)),
    )

    # 关键反例：意思相近但补充了新信息，**不能**合并
    added = palace.remember(
        "k3", "单笔不超过总资金三分之一，且同时持仓最多三只",
        tags=("仓位",), tier="fact", merge=True,
    )
    check(
        "语义相近但补了新信息 → 必须新建（合并会静默吃掉内容）",
        added.name == "k3" and len(palace.graph) == before + 1,
        f"返回={added.name} / nodes={len(palace.graph)}",
    )
    plain = MemoryPalace(clock=MutableClock(1_980_000_000.0))
    plain.remember("x1", "同一句话内容在这里", merge=False)
    plain.remember("x2", "同一句话内容在这里", merge=False)
    check(
        "merge 默认关闭：库层要可预测，去重是入口层的决定",
        len(plain.graph) == 2,
        f"nodes={len(plain.graph)}（默认不去重，两条都留着）",
    )


# ---------------- 3. 弃答 ----------------

def test_abstain() -> None:
    print("\n弃答：不知道就说不知道")
    palace, _ = fresh()
    palace.remember(
        "rule", "禁止编造版本号，不确定就说不确定",
        tags=("硬规则",), tier="core", pinned=True,
    )
    palace.remember("kelly", "单笔不超过总资金三分之一", tags=("仓位",), tier="fact")
    palace.remember("sector", "板块资金接力看主线与连板基因", tags=("选股",), tier="fact")

    hit = build_context(palace, query="仓位怎么配", budget=400)
    miss = build_context(palace, query="周末去哪玩比较好", budget=400)
    other = build_context(palace, query="帮我写个爬虫", budget=400)

    check(
        "命中时不弃答",
        hit["abstain"] is False and any(i["name"] == "kelly" for i in hit["items"]),
        f"abstain={hit['abstain']} items={[i['name'] for i in hit['items']]}",
    )
    check(
        "无关问题 → 弃答，且不拿「最重要的两条」凑数",
        miss["abstain"] is True
        and [i["name"] for i in miss["items"]] == ["rule"],
        f"abstain={miss['abstain']} items={[i['name'] for i in miss['items']]}",
    )
    check(
        "弃答时钉死的硬规则仍然注入（规则与问题无关也要遵守）",
        any(i["pinned"] for i in miss["items"]),
        str([(i["name"], i["pinned"]) for i in miss["items"]]),
    )
    check(
        "弃答会写进注入块，模型不可能看不到",
        "没有相关记忆" in format_context(miss),
        format_context(miss).splitlines()[-1][:40],
    )
    check(
        "换个无关问题同样弃答（不是只对某一个词生效）",
        other["abstain"] is True,
        f"abstain={other['abstain']}",
    )
    check(
        "弃答不影响正常问题的候选集（不是把检索门槛整体抬高）",
        [n.name for n in palace.recall(query="板块怎么跟")] and "sector" in
        [n.name for n in palace.recall(query="板块怎么跟")],
        str([n.name for n in palace.recall(query="板块怎么跟")]),
    )

    # 反事实：把弃答线降到 0，无关问题就会被硬凑
    loose = MemoryPalace(
        clock=MutableClock(1_980_000_000.0),
        config=PalaceConfig(abstain_semantic_floor=0.0),
    )
    loose.remember("rule", "禁止编造版本号", tags=("硬规则",), tier="core", pinned=True)
    loose.remember("kelly", "单笔不超过总资金三分之一", tags=("仓位",), tier="fact")
    loose_ctx = build_context(loose, query="周末去哪玩比较好", budget=400)
    check(
        "把弃答线降到 0，同一个问题就不再弃答（阈值确实在起作用）",
        loose_ctx["abstain"] is False and len(loose_ctx["items"]) > 1,
        f"abstain={loose_ctx['abstain']} items={[i['name'] for i in loose_ctx['items']]}",
    )


def main() -> int:
    test_bitemporal()
    test_bitemporal_counterfactual()
    test_dedupe()
    test_abstain()
    total = PASS + FAIL
    print(f"\n{'=' * 60}\n{total} 项断言：{PASS} 通过 / {FAIL} 失败\n{'=' * 60}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
