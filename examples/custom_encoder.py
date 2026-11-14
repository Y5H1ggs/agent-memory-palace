"""custom_encoder.py - 换掉默认的字符 bigram，接一个真语义编码器。

    python examples/custom_encoder.py

为什么需要这个示例：`encoder=` 这个参数一直在，但没文档、没示例、没测试，
于是"语义能力弱"看起来像"架构没设计扩展点"。**能力存在但没人知道，等于不存在。**

默认编码器是手写的「概念种子 + 字符 bigram」：零依赖、可解释、离线，
但它抓不到同义改写（"CI 日志乱码" vs "构建机输出问号"，余弦 0.06）。
这不是 bug，是取舍 —— 换成 embedding 就能解决，代价是引入模型依赖。

接入方式：实现 `encode(text) -> list[float]` 与 `dim`，传给 MemoryPalace，
**沉淀与检索会一起生效**（不是只改一半）。
"""
from __future__ import annotations

import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory_palace import MemoryPalace, MutableClock


class KeywordEncoder:
    """演示用的"概念编码器"：按主题词命中打分。

    真实项目里把 encode 换成：

        from sentence_transformers import SentenceTransformer
        model = SentenceTransformer("BAAI/bge-small-zh-v1.5")
        class BgeEncoder:
            dim = 512
            def encode(self, text):
                return model.encode(text, normalize_embeddings=True).tolist()

    或者任意 embedding API（记得 normalize，并保证 dim 稳定）。
    """

    dim = 4
    TOPICS = (
        ("ci", "构建", "windows", "乱码", "编码", "utf-8", "日志", "问号", "编译"),
        ("部署", "迁移", "上线", "回滚", "发布", "env", "migration", "release"),
        ("仓位", "凯利", "资金", "止损", "回撤"),
        ("表格", "格式", "输出", "精简", "啰嗦"),
    )

    def encode(self, text: str) -> list[float]:
        low = text.lower()
        vec = [sum(1.0 for w in words if w in low) for words in self.TOPICS]
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


def build(encoder=None, project: str = "demo") -> MemoryPalace:
    palace = MemoryPalace(
        clock=MutableClock(1_999_000_000.0), project=project, encoder=encoder
    )
    palace.remember("a", "构建机输出乱码，永远要手动设 UTF-8", tags=("build",), tier="fact")
    palace.remember("b", "CI 日志总是一堆问号，得先配好编码", tags=("ci",), tier="fact")
    palace.remember("c", "上线前先把数据库迁移跑完", tags=("deploy",), tier="fact")
    palace.remember("d", "输出用表格，别写长篇", tags=("style",), tier="fact")
    return palace


def main() -> None:
    query = "编码乱码怎么处理"
    print(f"查询：{query}\n")

    print("① 默认编码器（字符 bigram，零依赖）")
    base = build()
    print("   召回：", [n.name for n in base.recall(query=query)])
    clusters = base.consolidation._clusters(list(base.graph.all_nodes()), base.graph)
    print("   聚类：", [[m.name for m in c] for c in clusters])
    print("   → b 被排在后面（靠巧合，不是靠语义）")
    print("   → 更糟的是 a 与 d 被聚成一类：都含「输出」二字，字面像、意思完全不同")
    print("     这是 bigram 的**误判**，比抓不到同义更危险 —— 它会把无关的事沉淀成一条结论\n")

    print("② 注入 KeywordEncoder（概念命中）")
    better = build(KeywordEncoder())
    print("   召回：", [n.name for n in better.recall(query=query)])
    clusters = better.consolidation._clusters(list(better.graph.all_nodes()), better.graph)
    print("   聚类：", [[m.name for m in c] for c in clusters])
    print("   → a 与 b 被认成同一件事（不再靠共同的「输出」二字），可以沉淀")
    print("   → d 单独一类：正确，内容与编码无关\n")

    print("③ 换成真实 embedding（示例，需要装模型）")
    print(
        "   class BgeEncoder:\n"
        "       dim = 512\n"
        "       def encode(self, text):\n"
        "           return model.encode(text, normalize_embeddings=True).tolist()\n"
        "   MemoryPalace(encoder=BgeEncoder())\n"
    )
    print(
        "注意：换编码器后，**旧库需要重建向量索引**（向量不入盘，按内容重建），\n"
        "但节点与权重不受影响 —— 这是把向量排除在快照之外换来的好处。"
    )


if __name__ == "__main__":
    main()
