"""test_pragmatics.py - 语用层：一句话是什么行为，有多硬，为什么说。

    python tests/test_pragmatics.py

这一层最容易出的错不是"分类不准"，是**把不该记的记成了规矩**：

  · 把「随便你」记成「用户同意用表格」——用户放弃了沟通，系统当成拿到许可；
  · 把「表格是不是有点多」记成「禁止表格」——暗示被当成命令；
  · 把「我觉得可能不要」记成 core ——一句「可能」变成永久规矩；
  · 把「我上次说过别用表格」只记成「偏好表格」——真正的问题是"你没记住"，被消解了。

所以断言的重点不是「分对了」，是「**分错时会不会造成不可逆的存储后果**」。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory_palace import MemoryPalace, MutableClock
from memory_palace.pragmatics import Speech, classify, detect_register

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


def test_classification() -> None:
    print("\n分类：行为决定它该怎么被记住")
    cases = [
        ("以后都别用表格了", "prohibition"),
        ("禁止在报告里写绝对化措辞", "prohibition"),
        ("能否别用表格", "request"),          # 「能不能」是请求，不是禁令
        ("我上次说过别用表格", "correction"),  # 纠正优先于禁令
        ("随便你", "withdrawal"),
        ("算了，不用了", "withdrawal"),
        ("我更喜欢短段落", "preference"),
        ("必须每次都跑自测", "directive"),
        ("表格是不是有点多", "complaint"),
        ("对，就这样", "confirmation"),
        ("复盘要记录当时的判断", "statement"),
    ]
    wrong = [(text, want, classify(text).act) for text, want in cases if classify(text).act != want]
    check(
        "11 个典型说法都归到正确的言语行为",
        not wrong,
        f"错判 {wrong}" if wrong else f"{len(cases)} 例全对",
    )


def test_withdrawal_is_not_consent() -> None:
    print("\n最危险的一句：「随便你」不是同意")
    s = classify("随便你")
    check("它是撤回，不是偏好", s.act == "withdrawal", s.act)
    check(
        "**不能**被当作「用户表达了一个稳定偏好」",
        s.is_preference() is False,
        f"is_preference={s.is_preference()}",
    )
    check(
        "动机被标注为「放弃沟通」而不是「同意」",
        "不等于同意" in s.motive,
        s.motive,
    )
    check(
        "层级只到 event，不会进 core/fact",
        s.suggested_tier() == "event",
        s.suggested_tier(),
    )


def test_hint_is_not_rule() -> None:
    print("\n暗示与不满：记下来，但标「未确认」")
    s = classify("表格是不是有点多")
    check("归为不满（不是禁令）", s.act == "complaint", s.act)
    check("强度低（< 0.4）", s.force < 0.4, f"force={s.force}")
    check("不算偏好", s.is_preference() is False, "False")
    check("层级 event，不进 core", s.suggested_tier() == "event", s.suggested_tier())


def test_hedge_downgrades() -> None:
    print("\n模糊限定：一句「可能」不该变成永久规矩")
    hard = classify("以后都别用表格了")
    soft = classify("我觉得可能不要用表格")
    check(
        "同样命中禁令词，带模糊限定的强度明显更低",
        soft.force < hard.force - 0.2,
        f"硬={hard.force} 软={soft.force}",
    )
    check("模糊的那条被标 hedged", soft.hedged is True, "True")
    check(
        "层级降级：硬的进 core，软的最多到 event",
        hard.suggested_tier() == "core" and soft.suggested_tier() == "event",
        f"硬={hard.suggested_tier()} 软={soft.suggested_tier()}",
    )
    mid = classify("别用表格")
    check(
        "中间档：明确但不够绝对的禁令只到 fact（可协商）",
        mid.suggested_tier() == "fact",
        mid.suggested_tier(),
    )


def test_register() -> None:
    print("\n场景：同一句话在不同场合分量不同")
    check(
        "复盘语境被识别",
        detect_register("今天的复盘：不该追高") == "review",
        detect_register("今天的复盘：不该追高"),
    )
    check(
        "编码语境被识别",
        detect_register("部署脚本报错了") == "coding",
        detect_register("部署脚本报错了"),
    )
    check(
        "看不出场景就老实返回 chat（不硬猜）",
        detect_register("嗯") == "chat",
        detect_register("嗯"),
    )


def test_storage_integration() -> None:
    print("\n存储：默认只标注，显式要才改层级")
    clock = MutableClock(2_000_200_000.0)

    p = MemoryPalace(clock=clock, project="t")
    labeled = p.remember("a", "以后都别用表格了", tags=("格式",))
    check(
        "默认 auto_tier=False：只标注，不动调用方给的层级",
        labeled.tier == "event" and labeled.speech.act == "prohibition",
        f"tier={labeled.tier} act={labeled.speech.act}",
    )
    check(
        "但建议层级是对的（core），供调用方决定",
        labeled.speech.suggested_tier() == "core",
        labeled.speech.suggested_tier(),
    )

    auto = p.remember("b", "以后都别写长篇", tags=("格式",), auto_tier=True)
    check(
        "auto_tier=True 时禁令进 core",
        auto.tier == "core",
        auto.tier,
    )
    hinted = p.remember("c", "表格是不是有点多", tags=("格式",), auto_tier=True)
    check(
        "暗示即使 auto_tier 也只进 event（不许升级成规矩）",
        hinted.tier == "event",
        hinted.tier,
    )
    wd = p.remember("d", "随便你", tags=("格式",), auto_tier=True)
    check(
        "撤回同样只进 event",
        wd.tier == "event" and wd.speech.is_preference() is False,
        f"tier={wd.tier} pref={wd.speech.is_preference()}",
    )


def test_roundtrip_and_merge() -> None:
    print("\n持久化与合并：档案不能丢，也不能被更软的覆盖")
    import json
    import tempfile

    clock = MutableClock(2_000_300_000.0)
    p = MemoryPalace(clock=clock, project="t")
    p.remember("x", "以后都别用表格了", tags=("格式",), auto_tier=True)

    with tempfile.TemporaryDirectory(prefix="mp-prag-") as tmp:
        path = os.path.join(tmp, "s.json")
        p.save(path)
        raw = json.load(open(path, encoding="utf-8"))
        node = (raw["graph"]["nodes"] if isinstance(raw["graph"]["nodes"], list)
                else list(raw["graph"]["nodes"].values()))[0]
        check(
            "语用档案进了快照（act/force/register 都在）",
            node.get("speech", {}).get("act") == "prohibition",
            str(node.get("speech")),
        )
        reloaded = MemoryPalace(path=path, clock=clock, project="t")
        got = reloaded.graph.get("x")
        check(
            "重载后档案与层级都还在",
            got is not None and got.speech.act == "prohibition" and got.tier == "core",
            f"act={got.speech.act if got else None} tier={got.tier if got else None}",
        )

    q = MemoryPalace(clock=clock, project="t")
    # 这里必须让文本**几乎完全相同**，才真的会走合并路径。
    # 之前用「用表格呈现」vs「以后都别用表格了」—— 余弦不够、长度比也不够，
    # 实际是新建了一条，断言却绿了：典型的假绿（绿灯审计抓到的）。
    soft = Speech(act="statement", force=0.5)
    hard = Speech(act="prohibition", force=0.9)
    first = q.remember("y", "表格呈现一下", tags=("格式",), speech=soft)
    merged = q.remember("y2", "表格呈现一下", tags=("格式",), speech=hard, merge=True)
    check(
        "确实走了合并路径（没有新建节点）",
        merged.name == first.name,
        f"返回={merged.name}",
    )
    check(
        "合并时更硬的那次覆盖更软的（禁令不该被陈述吞掉）",
        merged.speech.act == "prohibition" and merged.speech.force > 0.8,
        f"act={merged.speech.act} force={merged.speech.force}",
    )


def test_counterfactual() -> None:
    print("\n反事实：关掉分类器，上面这些断言必须失效")
    clock = MutableClock(2_000_400_000.0)
    p = MemoryPalace(clock=clock, project="t")
    # 直接构造一个「恒陈述」的档案，绕开分类器：如果 tier 仍由文案决定，
    # 说明 auto_tier 实际上没在用 speech，那 auto_tier 就是装饰品。
    flat = Speech(act="statement", force=0.5, confidence=0.3)
    a = p.remember("z", "以后都别用表格了", tags=("格式",), speech=flat, auto_tier=True)
    check(
        "喂一个恒 statement 的档案时，禁令不会再进 core（证明 tier 真的由分类结果决定）",
        a.tier == "event",
        f"tier={a.tier}",
    )


def main() -> int:
    test_classification()
    test_withdrawal_is_not_consent()
    test_hint_is_not_rule()
    test_hedge_downgrades()
    test_register()
    test_storage_integration()
    test_roundtrip_and_merge()
    test_counterfactual()
    total = PASS + FAIL
    print(f"\n{'=' * 60}\n{total} 项断言：{PASS} 通过 / {FAIL} 失败\n{'=' * 60}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
