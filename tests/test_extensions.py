"""test_extensions.py - 两个扩展点的契约：语义编码器、外部反思。

    python tests/test_extensions.py

为什么单独测这两个：它们都存在过，但**没人知道它们存在** ——
`encoder=` 一直在 `MemoryPalace.__init__` 里，却没文档、没示例、没测试，
于是"语义弱"就成了"架构缺陷"，而不是"默认实现是 bigram，你可以换"。

所以这组断言的目的不是"覆盖代码"，是把隐含能力变成**契约**：
  1. 默认编码器做不了同义改写（诚实记录，不是缺陷）
  2. 换一个编码器后，沉淀与检索同时生效（不是只改一半）
  3. 外部反思能追加观察，但**不能改配置**，且失败不得炸穿一轮代谢
"""
from __future__ import annotations

import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory_palace import MemoryPalace, MutableClock
from memory_palace.semantics import cosine

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


class TopicEncoder:
    """玩具"真语义"编码器：只看概念命中，不看字面顺序。

    真实场景里换成 sentence-transformers / bge / 任意 embedding API，
    只要实现 `encode(text) -> list[float]` 与 `dim` 即可。
    """

    dim = 4
    TOPICS = (
        ("ci", "构建", "windows", "乱码", "编码", "utf-8", "日志", "问号"),
        ("部署", "迁移", "上线", "回滚", "发布", "env", "sh"),
        ("仓位", "凯利", "资金", "止损"),
        ("表格", "格式", "输出", "精简"),
    )

    def encode(self, text: str) -> list[float]:
        low = text.lower()
        vec = [sum(1.0 for w in words if w in low) for words in self.TOPICS]
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


A = "构建机输出乱码，永远要手动设 UTF-8"
B = "CI 日志总是一堆问号，得先配好编码"


def test_default_encoder() -> None:
    print("\n默认编码器：字面 bigram，能抓重复，抓不到同义")
    p = MemoryPalace(clock=MutableClock(1_998_000_000.0), project="t")
    p.remember("a", A, tags=("build",))
    p.remember("b", B, tags=("ci",))
    sim = cosine(p._vector_of(p.graph.get("a")), p._vector_of(p.graph.get("b")))
    check(
        "同义改写（字面不同）在默认编码器下余弦极低 —— 这是事实，不是 bug",
        sim < 0.2,
        f"cosine={sim:.3f}",
    )
    clusters = p.consolidation._clusters(list(p.graph.all_nodes()), p.graph)
    check(
        "因此它们不会被聚成一类（默认实现的边界）",
        all(len(c) < 2 for c in clusters),
        str([[m.name for m in c] for c in clusters]),
    )


def test_injected_encoder() -> None:
    print("\n注入编码器：沉淀与检索要一起生效，不能只改一半")
    p = MemoryPalace(
        clock=MutableClock(1_998_000_000.0), project="t", encoder=TopicEncoder()
    )
    p.remember("a", A, tags=("build",))
    p.remember("b", B, tags=("ci",))
    sim = cosine(p._vector_of(p.graph.get("a")), p._vector_of(p.graph.get("b")))
    check("同义改写余弦显著升高", sim > 0.8, f"cosine={sim:.3f}")

    clusters = p.consolidation._clusters(list(p.graph.all_nodes()), p.graph)
    check(
        "沉淀聚类生效（标签不同也能聚到一起）",
        any(len(c) >= 2 for c in clusters),
        str([[m.name for m in c] for c in clusters]),
    )
    hits = [n.name for n in p.recall(query="编码乱码")]
    check(
        "检索同样走注入的编码器（不是只改了沉淀）",
        len(hits) == 2,
        str(hits),
    )

    # 同一句话换了说法，用新编码器应当召回
    q = MemoryPalace(clock=MutableClock(1_998_000_000.0), project="t", encoder=TopicEncoder())
    q.remember("old", "上线前先把数据库迁移跑完", tags=("x",))
    q.remember("other", "用户偏好表格输出", tags=("y",))
    check(
        "换个说法也能召回正确条目",
        [n.name for n in q.recall(query="部署前要跑 migration")] == ["old"],
        str([n.name for n in q.recall(query="部署前要跑 migration")]),
    )


def test_encoder_robustness() -> None:
    print("\n坏编码器：不能炸穿，也不能写出维度不一致的索引")

    class Broken:
        dim = 3

        def encode(self, text: str) -> list[float]:
            raise RuntimeError("模型没加载")

    p = MemoryPalace(clock=MutableClock(1_998_000_000.0), project="t", encoder=Broken())
    try:
        p.remember("x", "任意一条", tags=("t",))
        crashed = False
    except Exception:  # noqa: BLE001
        crashed = True
    check(
        "编码器抛异常时写入要显式失败（不能写进一条检索不到的鬼记忆）",
        crashed,
        "抛异常" if crashed else "静默成功（危险）",
    )

    class WrongDim:
        dim = 8

        def encode(self, text: str) -> list[float]:
            return [0.1, 0.2]  # 只有 2 维

    q = MemoryPalace(clock=MutableClock(1_998_000_000.0), project="t", encoder=WrongDim())
    try:
        q.remember("y", "维度不对的一条", tags=("t",))
        ok = True
    except Exception:  # noqa: BLE001
        ok = False
    check("维度与声明不符时被拒绝，而不是写坏索引", ok is False, "被拒绝")


def test_reflector() -> None:
    print("\n外部反思：能追加观察，但不能改配置")

    seen: list[dict] = []

    def fake_llm(payload: dict) -> list[str]:
        seen.append(payload)
        return ["我最近总在同一类查询上召回失败，可能是标签打得过细。"]

    p = MemoryPalace(clock=MutableClock(1_998_000_000.0), reflector=fake_llm)
    for i in range(3):
        p.remember(f"n{i}", f"第 {i} 条复盘记录", tags=("复盘",))
    before = p._config.to_dict()
    p.run_cycle()
    after = p._config.to_dict()

    findings = p.last_report["findings"]
    check(
        "外部观察进入 findings，并带来源标记",
        any(f.startswith("〔外部反思〕") for f in findings),
        str([f for f in findings if "外部反思" in f])[:80],
    )
    check(
        "payload 里给足了上下文（指标 + 最近事件），不是只给一个分数",
        bool(seen) and "metrics" in seen[0] and "recent_events" in seen[0],
        f"keys={sorted(seen[0]) if seen else []}",
    )
    check(
        "外部反思**不能**直接改配置（只能提观察，参数仍由规则与自修正管）",
        before == after or all(
            before[k] == after[k] for k in before if not k.startswith("selfcorrect")
        ),
        "config 未被外部改动",
    )

    def broken(payload: dict) -> list[str]:
        raise RuntimeError("LLM 超时")

    q = MemoryPalace(clock=MutableClock(1_998_000_000.0), reflector=broken)
    for i in range(3):
        q.remember(f"m{i}", f"第 {i} 条", tags=("复盘",))
    q.run_cycle()
    check(
        "外部反思失败不炸穿一轮代谢（与 autosave 同一原则）",
        q.last_report is not None,
        "代谢完成",
    )
    check(
        "但错误必须可见，不能吞掉",
        q.reflection.reflector_error is not None
        and "RuntimeError" in q.reflection.reflector_error,
        str(q.reflection.reflector_error),
    )

    r = MemoryPalace(clock=MutableClock(1_998_000_000.0))
    for i in range(3):
        r.remember(f"k{i}", f"第 {i} 条", tags=("复盘",))
    r.run_cycle()
    check(
        "不传 reflector 时仍是规则化监控（默认零依赖）",
        r.last_report is not None
        and not any("外部反思" in f for f in r.last_report["findings"]),
        str(len(r.last_report["findings"])) + " 条规则 finding",
    )


def main() -> int:
    """**自动发现**所有 test_ 函数，不维护手写清单。

    手写清单出过一次事故：新加的用例忘了写进 main()，脚本模式跑完全绿，
    于是突变审计显示「改坏了也没人红」—— 而真相是那条用例根本没执行。
    自动发现没有这个失败模式。
    """
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn) and fn.__module__ == __name__:
            fn()
    total = PASS + FAIL
    print(f"\n{'=' * 60}\n{total} 项断言：{PASS} 通过 / {FAIL} 失败\n{'=' * 60}")
    return 0 if FAIL == 0 else 1


def test_style_suppression_after_adjudication() -> None:
    """裁决之后，输家必须真的从输出里消失。

    这条以前只在 demo 里演过；demo 重写后没人管它，突变审计立刻掉了一个
    （`style-no-suppress`：把 `if rule.suppressed_by is not None: return False`
    改成 `if False` 仍然全绿）。所以把它钉在测试里，而不是留在演示脚本里。
    """
    from memory_palace.palace import MemoryPalace

    palace = MemoryPalace()
    for _ in range(10):
        palace.observe_style("terse", "回答要短，先给结论", +1.0, 1.0, opposes=("verbose",))
    for _ in range(3):
        palace.observe_style("verbose", "回答要详尽，给全背景", +0.2, 1.0, opposes=("terse",))
    # 真实系统里由 age()/settle() 把它们推到固化态；这里直接置位，只测裁决与压制的因果关系
    for rule in palace.style.rules.values():
        rule.crystallized = True

    palace.style.adjudicate(palace.now(), emit=True)

    assert palace.style.rules["terse"].suppressed_by is None
    assert palace.style.rules["verbose"].suppressed_by == "terse", "输家没有被压制"
    active = palace.style.active_statements(palace.now())
    assert "回答要短，先给结论" in active
    assert "回答要详尽，给全背景" not in active, f"输家仍进了输出: {active}"
    # 被压制不等于「冲突消失」：报表上仍要看得见这场对立
    assert palace.style.conflicts(palace.now()) == [("terse", "verbose")]


if __name__ == "__main__":
    sys.exit(main())
