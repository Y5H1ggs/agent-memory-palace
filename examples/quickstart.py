"""quickstart.py - 60 行看完这套记忆系统在做什么。

运行（不装包也能跑）：python examples/quickstart.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory_palace import MemoryPalace, MutableClock  # noqa: E402

clock = MutableClock()  # 换成真实时钟就不传 clock；这里为了演示可控时间
palace = MemoryPalace(clock=clock)

# 1) 写入：情绪不用自己算 —— 文本里的「亏」「后悔」会自动带出效价与唤醒
palace.remember(
    "rule:no-absolute",
    "禁止绝对化措辞，所有判断都带概率",
    tags=("硬规则", "措辞"),
    tier="core",
    pinned=True,  # 钉死的规则：不衰减、不参与自动遗忘
)
palace.remember(
    "loss:0914",
    "追高之后逆板块阴跌，亏了三个点，很后悔",
    tags=("复盘", "教训"),
)
palace.remember(
    "kelly",
    "半凯利仓位：单笔不超过总资金三分之一",
    tags=("仓位",),
)
palace.remember(
    "sector",
    "板块资金接力看主线与连板基因，首板后分歧票容易被抛弃",
    tags=("选股",),
)

# 2) 检索：查询词和记忆的标签零重叠，也能靠语义命中
hits = palace.recall(query="仓位该怎么分配")
print("语义召回:", [n.name for n in hits])

# 3) 情绪是有来源的：不是调用方拍一个 0.9，而是文本里的词
affect = palace.graph.get("loss:0914").affect
print("情绪档案:", f"效价 {affect.valence:+.2f} / 唤醒 {affect.arousal:.2f} / 来源 {affect.source} / 依据 {affect.drivers}")

# 4) 熵有两个维度：价值分布（是不是什么都一样重要）+ 图的结构（是不是一盘散沙）
print("价值健康:", palace.entropy()["health"], "/ 结构:", palace.entropy()["structure_health"])

# 5) 走一轮完整代谢：沉淀 → 抓取 → 遗忘 → 自述 → 人格 → 反思 → 自修正
report = palace.run_cycle()
print("本轮自述:", report["narrative"]["text"][:60], "…")

# 6) 多义词归属可查：同一个「资金」，在板块语境与仓位语境里归不同概念
print("多义词裁决:", palace.semantic_explain("板块资金接力")["ambiguous"])

# 7) 每条记忆都有判决与理由：强度、情绪、凭据、叙事地位四个分量都在账上
print("最近遗忘判决:", [(d["name"], d["verdict"], d["reason"]) for d in palace.last_gc])
