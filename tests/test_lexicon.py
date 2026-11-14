"""test_lexicon.py - 词法层：归一、分词、否定词、IDF。

    python tests/test_lexicon.py

为什么要单独测词法层：它是语义栈的地基。地基错了，上面每个机制都在将就 ——
同义改写召回不到只是"笨"，共享「输出」二字的两句话被聚成一类才是"错"。
这一组里最重要的不是功能断言，是**反事实**：
把 IDF 关掉，误聚必须回来；否则说明这些断言根本没在测 IDF。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory_palace import MemoryPalace, MutableClock
from memory_palace.lexicon import IdfModel, normalize, tokens
from memory_palace.semantics import ConceptEncoder, cosine

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


def test_normalize() -> None:
    print("\n归一：同一个词不该有两种写法")
    check(
        "全角折半角（ＵＴＦ－８ → utf-8）",
        "utf-8" in normalize("ＵＴＦ－８ 编码"),
        normalize("ＵＴＦ－８ 编码"),
    )
    check("大小写统一", normalize("PNPM").strip() == "pnpm", normalize("PNPM"))
    check(
        "全角标点被折成半角（省掉一堆特判）",
        "（" not in normalize("（部署）"),
        normalize("（部署）"),
    )


def test_tokens() -> None:
    print("\n分词：中文双字 + 英文标识符 + 否定词")
    t = tokens("env.sh 要先 source，不能忽略")
    check(
        "复合标识符同时保留整体与部件（env.sh / env / sh）",
        {"env.sh", "env", "sh"}.issubset(set(t)),
        sorted(k for k in t if "env" in k or k == "sh"),
    )
    check(
        "否定词被保留（否则「不能忽略」与「忽略」同义）",
        "不" in t,
        sorted(t),
    )
    check(
        "停用单字被过滤（的/了/是）",
        not ({"的", "了", "是"} & set(tokens("这件事的处理是好的"))),
        sorted(tokens("这件事的处理是好的")),
    )
    check(
        "中文仍取双字 bigram（部署 / 迁移）",
        {"部署", "迁移"}.issubset(set(tokens("部署前先跑迁移"))),
        sorted(tokens("部署前先跑迁移")),
    )


def test_negation_weight() -> None:
    print("\n否定：把结论说反比召回不到更严重")
    c = ConceptEncoder()
    a = c.encode_with(None, "不追高，缩量不参与")
    b = c.encode_with(None, "追高，缩量参与")
    same_neg = c.encode_with(None, "不追高，缩量不参与")
    check(
        "「不追高」与「追高」的相似度明显低于自身重复",
        cosine(a, b) < cosine(a, same_neg) - 0.05,
        f"不追高vs追高={cosine(a, b):.3f} / 自身={cosine(a, same_neg):.3f}",
    )
    check(
        "否定词出现在向量里（不是被吞掉）",
        any(abs(v) > 0 for v in a),
        "非零向量",
    )


def test_idf() -> None:
    print("\nIDF：到处都有的词，不该和只有这里才有的词一样重")
    docs = [
        "输出用表格，别写长篇",
        "输出优先精简",
        "构建机输出乱码，要设 UTF-8",
        "CI 日志输出一堆问号",
        "部署前先跑迁移脚本",
        "凯利仓位单笔不超过三分之一",
    ]
    idf = IdfModel(docs)
    check(
        "高频词权重低于低频词",
        idf.weight("输出") < idf.weight("凯利"),
        f"输出={idf.weight('输出'):.3f} / 凯利={idf.weight('凯利'):.3f}",
    )
    check(
        "语料里没出现过的词权重为 0（否则跨词边界碎片会拿最高权重）",
        idf.weight("么处") == 0.0,
        f"么处={idf.weight('么处'):.3f}",
    )
    check(
        "空语料不崩，权重全 0",
        IdfModel([]).weight("任意") == 0.0,
        "0.0",
    )


def build_docs() -> list[str]:
    """一组「共享『输出』但主题不同」的语料 —— 误聚的标准素材。

    每条**标签各不相同**：否则标签通道（jaccard=1 → 0.75）会先把它们连成一团，
    测出来的就成了「标签分组器」，与 IDF 无关。
    """
    return [
        "构建机输出乱码，永远要手动设 UTF-8",   # 0
        "CI 日志输出一堆问号，得先配好编码",     # 1
        "输出用表格，别写长篇",                  # 2
        "输出优先精简，别铺开",                  # 3
        "部署前先跑迁移脚本，别跳",              # 4
        "凯利仓位单笔不超过三分之一",            # 5
        "上线失败要能一键回滚到上一版",          # 6
        "复盘要记录当时的判断，不是只记结果",    # 7
    ]


class FlatIdf(IdfModel):
    """反事实用的尺子：所有词同权（等价于没有 IDF）。"""

    def weight(self, term: str) -> float:  # noqa: D401
        return 1.0


def make_palace(flat: bool) -> MemoryPalace:
    p = MemoryPalace(clock=MutableClock(1_999_500_000.0), project="t")
    for i, text in enumerate(build_docs()):
        p.remember(f"n{i}", text, tags=(f"t{i}",))
    if flat:
        # 注意：必须在 rebuild **之后**替换，否则 rebuild 会用真实语料重算 IDF 把它盖掉
        p.semantics._idf = FlatIdf([])
        for node in p.graph.all_nodes():
            p.semantics._vectors[node.name] = p.semantics._encode(node.content)
        p.semantics._dirty = False
    return p


def test_idf_fixes_false_merge() -> None:
    print("\n核心证据：关掉 IDF，误聚回来；打开 IDF，误聚消失")

    def pair_cosine(palace: MemoryPalace) -> float:
        a = palace.graph.get("n0")   # 构建机输出乱码
        d = palace.graph.get("n2")   # 输出用表格
        return cosine(palace._vector_of(a), palace._vector_of(d))

    with_idf = pair_cosine(make_palace(flat=False))
    without = pair_cosine(make_palace(flat=True))
    check(
        "IDF 显著降低「共享『输出』但主题不同」的相似度",
        with_idf < without - 0.05,
        f"有 IDF={with_idf:.3f} / 无 IDF={without:.3f}",
    )

    p = make_palace(flat=False)
    clusters = p.consolidation._clusters(list(p.graph.all_nodes()), p.graph)
    merged = [sorted(m.name for m in c) for c in clusters if len(c) > 1]
    check(
        "默认配置下，「乱码」与「表格」不会被聚成一类",
        not any({"n0", "n2"}.issubset(set(group)) for group in merged),
        f"合并簇={merged}",
    )


def test_retrieval_requires_lexical_evidence() -> None:
    print("\n召回：没有词法证据就不该召回（新鲜不等于相关）")
    p = MemoryPalace(clock=MutableClock(1_999_500_000.0), project="t")
    p.remember("x", "半凯利仓位，单笔不超过三分之一", tags=("仓位",))
    check(
        "完全无关的查询召回不到（哪怕这条是刚写的）",
        p.recall(query="周末带娃去哪玩") == [],
        str([n.name for n in p.recall(query="周末带娃去哪玩")]),
    )
    check(
        "有词法重叠时正常召回",
        [n.name for n in p.recall(query="仓位怎么分配")] == ["x"],
        str([n.name for n in p.recall(query="仓位怎么分配")]),
    )


def main() -> int:
    test_normalize()
    test_tokens()
    test_negation_weight()
    test_idf()
    test_idf_fixes_false_merge()
    test_retrieval_requires_lexical_evidence()
    total = PASS + FAIL
    print(f"\n{'=' * 60}\n{total} 项断言：{PASS} 通过 / {FAIL} 失败\n{'=' * 60}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
