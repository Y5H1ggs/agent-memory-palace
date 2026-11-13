"""showcase.py - 生成一份「真实感」的记忆库，并渲染成可发布的面板。

    python examples/showcase.py                 # 写入 examples/showcase-store.json + docs/sample-palace.html
    python examples/showcase.py --days 180

为什么不用 12 条手写数据：演示太薄会让人以为这只是个玩具。
这里模拟 **120 天**的一次真实使用：一边写入、一边检索、一边沉淀、一边遗忘，
中途人格/风格也在固化。跑完之后你会看到：

  · 有 3 条钉死的硬规则（永不衰减，曲线是平的）
  · 有若干条「当时很痛」的记忆（情绪强 → 更难忘，但也标了「可能失真」）
  · 有被沉淀成结论的核心节点（由多条碎片聚合而来）
  · 有真正被放手的东西（墓碑里有名字，不是空列表）
  · 自述演进过多版（章节 + 单主题占比 + 可靠度）

内容刻意做成**通用 agent 场景**（编码、发布、用户偏好），不含任何具体公司、股票或个人信息 ——
演示数据也要能直接见人。
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory_palace import MemoryPalace, MutableClock, PalaceConfig  # noqa: E402
from memory_palace.logic import Relation  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

RULES = [
    ("rule:no-fabricate", "不要编造版本号或 API 参数，不确定就说不确定", ("硬规则", "诚实"), "core", True),
    ("rule:schema", "改数据结构必须同时升状态版本并写迁移，否则旧快照会加载失败", ("硬规则", "工程"), "core", True),
    ("rule:test-first", "说「完成」之前先跑一遍自测，绿了才算完成", ("硬规则", "工程"), "core", True),
    ("rule:no-emoji", "正式文档不用 emoji 装饰", ("硬规则", "格式"), "fact", True),
]

PREFERENCES = [
    ("pref:table", "能用表格就不用段落", ("偏好", "格式"), "fact"),
    ("pref:concise", "先给结论再给理由，不要铺垫", ("偏好", "格式"), "fact"),
    ("pref:cn-comment", "注释写中文，变量名用英文", ("偏好", "工程"), "fact"),
    ("pref:no-flattery", "不要奉承式开场", ("偏好", "语气"), "fact"),
]

# (内容, 标签, 情绪强度, 说明)
WORK = [
    ("写了一个把 JSON 拆成树的小工具，比想象中好用", ("工具",), 0.2, "编码"),
    ("把长函数拆开之后可读性明显变好", ("重构", "习惯"), 0.3, "编码"),
    ("给接口加了超时，之前卡住是因为默认无限等", ("踩坑", "工程"), 0.6, "踩坑"),
    ("缓存键忘了带用户名，两个账号互相串数据", ("踩坑", "教训"), 0.75, "踩坑"),
    ("日志里打了完整凭据，被安全扫描抓出来", ("踩坑", "安全"), 0.85, "踩坑"),
    ("线上回滚了一次，原因是迁移脚本没在测试库跑过，很痛", ("复盘", "教训"), 0.9, "痛点"),
    ("CI 在 Windows 上因为控制台编码炸了，排查两小时", ("复盘", "教训"), 0.8, "痛点"),
    ("把并发测试的断言写成「没报错」，等于没测", ("复盘", "教训"), 0.7, "痛点"),
    ("测试用例自己写错了：用常数学习率制造「无惯性」，结果测不出来", ("复盘", "教训"), 0.65, "痛点"),
    ("把配置项逐字段对拍了一遍，发现两个字段被静默吞掉", ("复盘", "教训"), 0.55, "复盘"),
    ("先写失败用例再修，比盯着代码猜快得多", ("方法", "习惯"), 0.45, "方法"),
    ("给每个结论都标了概率，读者反馈说清楚多了", ("方法", "沟通"), 0.4, "方法"),
    ("把「查不到」和「没有」分开说，避免了两次误判", ("方法", "诚实"), 0.5, "方法"),
    ("周三下午例会，同步了一下进度", ("琐事",), 0.05, "琐事"),
    ("同事问的那个正则问题其实用不着正则", ("琐事",), 0.1, "琐事"),
    ("午饭时想到一个更简单的拆包方式", ("琐事",), 0.15, "琐事"),
    ("客户希望导出 PDF，先确认了字体嵌入", ("交付",), 0.3, "交付"),
    ("把 README 的安装步骤从 9 行压到 3 行", ("文档", "交付"), 0.35, "交付"),
    ("给示例代码加了断言，跑不通就不会静默通过", ("文档", "工程"), 0.4, "交付"),
    ("把错误信息从「失败」改成「哪一行、为什么、怎么改」", ("体验", "工程"), 0.5, "体验"),
    ("搜了半小时才发现官方文档里就写着", ("踩坑", "教训"), 0.6, "踩坑"),
    ("把重复的三段脚本合并成一个，少了两处不一致", ("重构", "工程"), 0.4, "重构"),
    ("漏了一个空输入的分支，整个批处理挂掉", ("踩坑", "教训"), 0.7, "踩坑"),
    ("给定时任务加了幂等，重跑不会再重复写入", ("工程", "方法"), 0.5, "工程"),
    ("把 300 行的单文件拆成四个模块，编译时间没变但好读多了", ("重构",), 0.35, "重构"),
    ("用户说「别解释了，直接给方案」，记下来", ("偏好", "沟通"), 0.4, "偏好"),
    ("发现自己在重复解释同一个概念，写成了短文档", ("文档", "方法"), 0.45, "文档"),
    ("把「不确定」写进结论，反而更容易被信任", ("诚实", "沟通"), 0.5, "诚实"),
    ("压测时连接池打满，扩了一倍才稳住", ("性能", "工程"), 0.6, "性能"),
    ("把一次慢查询从 1.2 秒压到 90 毫秒，加了索引", ("性能", "工程"), 0.55, "性能"),
    ("批量任务改成流式处理，内存占用从 2G 降到 200M", ("性能", "工程"), 0.6, "性能"),
    ("给关键路径加了埋点，终于知道慢在哪了", ("性能", "方法"), 0.5, "性能"),
    ("删掉了三个「以防万一」的配置项，没人发现", ("重构", "简化"), 0.4, "简化"),
    ("把默认值写进文档，省掉了一轮问答", ("文档", "体验"), 0.35, "文档"),
    ("给失败路径补了测试，覆盖率从 61% 到 84%", ("测试", "工程"), 0.5, "测试"),
    ("发现测试里有依赖执行顺序的用例，改成各自独立", ("测试", "教训"), 0.55, "测试"),
    ("周五的发布推迟到下周一，因为周五出事没人修", ("流程", "教训"), 0.6, "流程"),
    ("给回滚写了脚本，演练了两次才敢上线", ("流程", "工程"), 0.55, "流程"),
    ("把「先问清楚再动手」写进了团队约定", ("流程", "方法"), 0.45, "流程"),
]

STYLE_EVIDENCE = [
    ("concise", "输出尽量精简，先结论后理由", 3),
    ("tables", "能用表格就用表格", 3),
    ("verbose", "复杂问题要展开讲清楚", 2),
]


def build(days: int = 120) -> MemoryPalace:
    # 锚到真实时间：演示库要被 save / load / render 反复读，
    # 用「未来的模拟时间」会让重载时的衰减和渲染时对不上（踩过）。
    start = time.time() - (days + 1) * 86400
    clock = MutableClock(start)
    # 演示库的取舍比默认更宽容一点：真实使用里，多数结论不会两周就被丢掉。
    # 这是**演示参数**，不是系统默认值 —— 图中标注的数字要能对得上。
    palace = MemoryPalace(clock=clock, config=PalaceConfig(
        importance_forget=0.12,
        forget_w=0.02,
        consolidate_similarity=0.42,
        grasp_budget=4.0,
    ))

    for name, text, tags, tier, pinned in RULES:
        palace.remember(name, text, tags=tags, tier=tier, pinned=pinned)
    for name, text, tags, tier in PREFERENCES:
        palace.remember(name, text, tags=tags, tier=tier)

    # 逻辑关系：两条硬规则互相支撑，一条经验与「先跑再说」冲突
    palace.assert_relation("rule:test-first", "rule:schema", Relation.ENTAILS, 0.8)

    cursor = 0
    per_day = max(1, len(WORK) // max(1, days // 3))
    for day in range(days):
        if day % 2 == 0:  # 两天写一条（约 60 条，才像真的用过）
            if cursor < len(WORK):
                text, tags, intensity, kind = WORK[cursor]
                cursor += 1
                valence = -intensity if kind in ("踩坑", "痛点") else intensity * 0.6
                palace.remember(
                    f"d{day:03d}:{cursor}",
                    text,
                    tags=tags,
                    tier="event",
                    valence=valence,
                    arousal=intensity,
                )
        if day % 5 == 0:
            # 制造共现：同一主题的记忆被一起想起，才能沉淀
            names = [n.name for n in palace.graph.all_nodes() if "教训" in n.tags][-4:]
            if names:
                palace.recall(names=names)
        if day % 7 == 0:
            names = [n.name for n in palace.graph.all_nodes() if "格式" in n.tags][:3]
            if names:
                palace.recall(names=names)
        if day % 4 == 0:
            # 有些东西是天天用的：硬规则与结论被反复想起，权重自然拉开梯度。
            # 没有这一步，所有记忆重量一样，面板上就会显示「无梯度」—— 那是演示的假象，不是系统的特性。
            used = [n.name for n in palace.graph.all_nodes()
                    if n.pinned or n.tier in ("core", "fact")][:6]
            if used:
                palace.recall(names=used)
        if day % 6 == 0:
            for key, statement, _n in STYLE_EVIDENCE:
                palace.observe_style(
                    key, statement, +1, opposes=("concise",) if key == "verbose" else ()
                )
        if day in (30, 60, 90, 119):
            palace.run_cycle()
        if day in (45, 75, 105):
            palace.tell()  # 中途自述几次：被反复讲述的条目会拿到叙事地位
        clock.advance(1)

    # 给几条结论补上「凭据」：被硬规则推出来的记忆，重要度本来就该更高。
    # 没有这一步，所有条目的四个分量几乎相同，面板会诚实地显示「无梯度」——
    # 那不是 bug，是「这套记忆还没形成取舍」。
    assessment = palace.assessment()
    ranked = [n for n in sorted(palace.graph.all_nodes(),
                                key=lambda x: -assessment[x.name]["total"])
              if not n.pinned][:3]
    for node in ranked:
        palace.assert_relation("rule:test-first", node.name, Relation.ENTAILS, 0.7)

    # 用户纠正过一次判断 → 情绪反馈（不改强度）
    victims = sorted(palace.graph.nodes())
    if victims:
        palace.feel_outcome(victims[-1], "contradicted")
    palace.run_cycle()
    return palace


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成展示用记忆库与面板")
    parser.add_argument("--days", type=int, default=120)
    parser.add_argument("--store", default=os.path.join(ROOT, "examples", "showcase-store.json"))
    parser.add_argument("--out", default=os.path.join(ROOT, "docs", "sample-palace.html"))
    args = parser.parse_args(argv)

    import shutil

    tmp = args.store + ".tmp.json"
    palace = build(args.days)
    palace.save(tmp)
    shutil.move(tmp, args.store)

    from memory_palace.render import html_report, text_report

    with open(args.out, "w", encoding="utf-8") as f:
        f.write(html_report(palace, title="记忆宫殿 · 120 天演化"))
    print(text_report(palace))
    print(f"\n已写入 {args.store}")
    print(f"已渲染 {args.out}（{os.path.getsize(args.out) / 1024:.1f} KB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
