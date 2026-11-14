"""make_diagrams.py - 生成 README 里的说明图（SVG，可复现）。

    python examples/make_diagrams.py

为什么用脚本画图：手绘的图会随代码腐烂。这里每张图都由数据结构和坐标算出来，
改个数字重跑一次即可；图上的数值都在 `NUMBERS` 里，注明出自哪条命令。

产出（docs/images/）：
    hero.svg         它是什么 / 不是什么（左右对照）
    lifecycle.svg    一轮代谢的次序，以及每一步的判据
    storage.svg      SQLite 的七张表与一次提交的顺序
    concurrency.svg  两个进程同时写同一份记忆：为什么不会丢更新
    bench.svg        写入/读取延迟与磁盘占用（JSON vs SQLite）
    eval.svg         组件级评测：两条通道在两类查询上的召回

样式统一：浅色卡片 + 深色文字。GitHub 深色主题下也读得清 ——
不用 `prefers-color-scheme`，因为 SVG 作为 <img> 加载时媒体查询不可靠。
"""
from __future__ import annotations

import os

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "images")

# 全部数字的来源（2026-11-14，本机，Windows / Python 3.13）：
#   python -m memory_palace.bench 1000
#   python -m memory_palace.cli eval --channel hybrid
NUMBERS = {
    "json_write_p50": 28.2,
    "json_write_p95": 47.7,
    "sqlite_write_p50": 1.2,
    "sqlite_write_p95": 2.1,
    "json_load": 44.9,
    "sqlite_load": 45.7,
    "json_size": 2.0,
    "sqlite_size": 4.5,
    "lex_finance_exact": 0.889,
    "lex_finance_para": 0.591,
    "hyb_finance_exact": 0.889,
    "hyb_finance_para": 0.727,
    "lex_tech_exact": 1.000,
    "lex_tech_para": 0.923,
    "hyb_tech_exact": 1.000,
    "hyb_tech_para": 0.923,
}

INK = "#1f2328"
MUTED = "#57606a"
LINE = "#d0d7de"
PAPER = "#f6f8fa"
GREEN = "#1a7f37"
GREEN_BG = "#e9f5ec"
AMBER = "#9a6700"
AMBER_BG = "#fdf3dc"
BLUE = "#0969da"
BLUE_BG = "#e8f1fd"
RED = "#cf222e"
PURPLE = "#8250df"
PURPLE_BG = "#f3eefc"
WHITE = "#ffffff"

FONT = (
    "-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Hiragino Sans GB',"
    "'Microsoft YaHei',sans-serif"
)
MONO = "ui-monospace,SFMono-Regular,Menlo,Consolas,'Cascadia Mono',monospace"


def esc(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def svg(width: int, height: int, body: str, title: str) -> str:
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}" role="img" aria-label="{esc(title)}">
<title>{esc(title)}</title>
<rect x="0" y="0" width="{width}" height="{height}" fill="{WHITE}"/>
{body}
</svg>
"""


def text(x, y, s, size=14, fill=INK, weight=400, anchor="start", mono=False, ls=0):
    family = MONO if mono else FONT
    spacing = f' letter-spacing="{ls}"' if ls else ""
    return (
        f'<text x="{x}" y="{y}" font-family="{family}" font-size="{size}" fill="{fill}" '
        f'font-weight="{weight}" text-anchor="{anchor}"{spacing}>{esc(s)}</text>'
    )


def card(x, y, w, h, fill=WHITE, stroke=LINE, r=12, dash=None, width=1.2):
    dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
    return (
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" fill="{fill}" '
        f'stroke="{stroke}" stroke-width="{width}"{dash_attr}/>'
    )


def arrow(x1, y1, x2, y2, color=MUTED, width=1.6, dash=None, head=True):
    marker = ' marker-end="url(#arrowhead)"' if head else ""
    dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
    return (
        f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{color}" '
        f'stroke-width="{width}"{dash_attr}{marker}/>'
    )


def defs() -> str:
    return f"""<defs>
<marker id="arrowhead" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto">
  <path d="M0,0 L10,5 L0,10 z" fill="{MUTED}"/>
</marker>
<marker id="arrowgreen" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto">
  <path d="M0,0 L10,5 L0,10 z" fill="{GREEN}"/>
</marker>
</defs>"""


def chip(x, y, label, fill=GREEN_BG, stroke=GREEN, color=GREEN, pad=12, size=12.5):
    width = len(label) * (size * 0.95) + pad * 2
    return card(x, y, width, 26, fill=fill, stroke=stroke, r=13), text(
        x + pad, y + 18, label, size=size, fill=color, weight=600
    ), width


# ---------------------------------------------------------------- hero


def hero() -> str:
    body = [defs()]
    body.append(text(40, 46, "memory-palace", 26, INK, 700))
    body.append(
        text(40, 74, "会遗忘、会被情绪加固、会讲自己故事的记忆系统 · 零第三方依赖", 14.5, MUTED)
    )

    # 左：普通向量库
    body.append(card(40, 104, 520, 244, fill=PAPER, stroke=LINE))
    body.append(text(64, 136, "普通向量库 / RAG", 15, MUTED, 700))
    steps = [("存", "全量入库，只增不减"), ("检", "每次全库相似度检索"), ("塞", "把最像的几条塞进 prompt")]
    for i, (k, desc) in enumerate(steps):
        y = 158 + i * 58
        body.append(card(64, y, 472, 44, fill=WHITE))
        body.append(text(80, y + 28, k, 15, MUTED, 700))
        body.append(text(112, y + 28, desc, 13.5, INK))
    body.append(text(64, 336, "没有时间、没有取舍、也没有「当时是什么」", 12.5, MUTED))

    # 右：memory-palace
    body.append(card(640, 104, 520, 244, fill=GREEN_BG, stroke="#8cc7a0"))
    body.append(text(664, 136, "memory-palace", 15, GREEN, 700))
    steps2 = [
        ("写", "情绪与语用由文本推断，带依据"),
        ("算", "权重 = 锚点 + 时间（纯函数，幂等）"),
        ("选", "沉淀 / 遗忘 / 抓取，每条判决都有理由"),
        ("给", "按预算注入：树状，折叠显式标注"),
    ]
    for i, (k, desc) in enumerate(steps2):
        y = 150 + i * 46
        body.append(card(664, y, 472, 36, fill=WHITE, stroke="#b7dcc3"))
        body.append(text(680, y + 23, k, 14, GREEN, 700))
        body.append(text(712, y + 23, desc, 12.8, INK))
    body.append(text(664, 336, "旧事实会到期，不是被删 —— 所以「当时我是这么想的」也留得住", 12.2, MUTED))

    # 中间箭头
    body.append(arrow(578, 226, 626, 226, GREEN, 2))

    # 底部三个标签
    x = 40
    for label in ("零依赖（stdlib only）", "SQLite / WAL · 行级增量", "MCP · HTTP · CLI"):
        shape, label_text, width = chip(x, 372, label, BLUE_BG, BLUE, BLUE)
        body.append(shape)
        body.append(label_text)
        x += width + 12
    body.append(text(40, 424, "1e4 条量级 · 单机 · 单写者（多进程可排队写同一份记忆）", 12, MUTED))
    return svg(1200, 448, "\n".join(body), "memory-palace 是什么")


# ---------------------------------------------------------------- lifecycle


def lifecycle() -> str:
    body = [defs()]
    body.append(text(40, 44, "一轮代谢：次序本身是设计", 20, INK, 700))
    body.append(
        text(40, 70, "沉淀必须早于遗忘（刚要结晶的碎片不能被先扫走）；自述必须晚于遗忘（不能讲到已经不在的东西）", 13, MUTED)
    )

    steps = [
        ("写入", "情绪从文本/结果推断\n语用判断：禁令 / 请求 / 撤回", GREEN),
        ("沉淀", "标签同源 或 内容同义\n单向门：碎片降级为溯源", AMBER),
        ("抓取", "预算内留下最该留的\n被抓住的不会被遗忘", BLUE),
        ("遗忘", "结论按价值、碎片按强度\n钉死的一律不删", RED),
        ("自述", "投影而非节点\n叙事地位会随时间退潮", PURPLE),
        ("人格", "基座只读 / 人格生长\n人设可换，变更留痕", PURPLE),
        ("反思", "指标化回看 + 可选 LLM\n只提建议，不改配置", AMBER),
        ("自修正", "白名单 + 值域 + 护栏\n可回滚，反复改错就交还给人", GREEN),
    ]
    w, h, gap = 250, 96, 24
    # 蛇形排布：第二行从右往左，最后一步回到第一步，形成闭环。
    # 第一行左→右，第二行右→左；否则读者会以为第二行是「重新开始」而不是「继续」。
    for index, (name, desc, color) in enumerate(steps):
        row, col = divmod(index, 4)
        if row == 1:
            col = 3 - col
        x = 40 + col * (w + gap)
        y = 100 + row * (h + 46)
        body.append(card(x, y, w, h, fill=WHITE, stroke=color, width=1.4))
        body.append(text(x + 18, y + 30, f"{index + 1}. {name}", 15.5, color, 700))
        for line_index, line in enumerate(desc.split("\n")):
            body.append(text(x + 18, y + 56 + line_index * 20, line, 12.3, MUTED))
        if row == 0 and col < 3:
            body.append(arrow(x + w + 4, y + h / 2, x + w + gap - 6, y + h / 2, color))
        if row == 1 and col > 0:
            body.append(
                arrow(
                    x - gap + 6,
                    y + h / 2,
                    x - 4,
                    y + h / 2,
                    color,
                    head=False,
                )
            )
            body.append(f'<path d="M{x - gap + 6},{y + h / 2 - 5} L{x - gap + 1},{y + h / 2} L{x - gap + 6},{y + h / 2 + 5} z" fill="{color}"/>')

    # 第 4 步 → 第 5 步：同一列，直接向下
    last_x = 40 + 3 * (w + gap) + w / 2
    body.append(arrow(last_x, 100 + h + 4, last_x, 242 - 4, MUTED))
    # 第 8 步 → 第 1 步：闭环
    body.append(arrow(40 + w / 2, 242 - 4, 40 + w / 2, 100 + h + 4, GREEN, 1.8))
    body.append(text(40 + w / 2 + 14, 224, "下一轮从这里继续", 11.5, GREEN))
    return svg(1200, 392, "\n".join(body), "一轮代谢的次序")


# ---------------------------------------------------------------- storage


def storage() -> str:
    body = [defs()]
    body.append(text(40, 44, "存储：SQLite（WAL 模式），一条记忆一行", 20, INK, 700))
    body.append(text(40, 70, "默认后端。JSON 仍在（--backend json），作为可移植的单文件快照与冷备。", 13, MUTED))

    tables = [
        ("nodes", "一条记忆一行 + 4 个索引", GREEN),
        ("edges", "共现边：(a,b) 主键", GREEN),
        ("tombstones", "被显式丢弃的记忆（可恢复）", AMBER),
        ("blobs", "引擎状态：config / 叙事 / 人格 / 自修正", BLUE),
        ("events", "只追加。内存会 trim，库里不会", BLUE),
        ("commits", "提交账本：谁、何时、为什么、写了多少行", PURPLE),
        ("snapshots", "每 50 次提交留一份完整快照", PURPLE),
    ]
    for index, (name, desc, color) in enumerate(tables):
        y = 96 + index * 42
        body.append(card(40, y, 560, 34, fill=WHITE, stroke=LINE))
        body.append(text(56, y + 22, name, 13.5, color, 700, mono=True))
        body.append(text(150, y + 22, desc, 12.5, MUTED))

    # 右侧：一次提交的顺序
    body.append(card(636, 100, 524, 268, fill=PAPER, stroke=LINE))
    body.append(text(660, 130, "一次提交的顺序（不可交换）", 14.5, INK, 700))
    seq = [
        ("BEGIN IMMEDIATE", "第一毫秒就声明「我是写者」，不给锁升级留窗口"),
        ("读到别人的提交？", "比 meta.revision 新 → 先重载，再改"),
        ("只写脏行", "节点带 _dirty，改一条 = 1 行"),
        ("INSERT commits", "记账：原因、写者、写入行数、摘要"),
        ("COMMIT", "WAL + synchronous=FULL，落盘后才返回"),
    ]
    for index, (code, desc) in enumerate(seq):
        y = 152 + index * 42
        body.append(text(660, y, f"{index + 1}", 12, MUTED, 700, mono=True))
        body.append(text(680, y, code, 12.5, GREEN, 700, mono=True))
        body.append(text(680, y + 17, desc, 11.8, MUTED))
    body.append(text(40, 420, "写一次 = 一次行级增量 + 一条审计记录；改一条的代价与库的大小基本无关。", 12.5, MUTED))
    return svg(1200, 442, "\n".join(body), "SQLite 存储结构")


# ---------------------------------------------------------------- concurrency


def concurrency() -> str:
    body = [defs()]
    body.append(text(40, 44, "两个进程同时写同一份记忆：为什么不丢更新", 20, INK, 700))
    body.append(text(40, 70, "关键只有一条：重载发生在修改之前。反过来（先改后查）两边都认为自己最新，后写的整段覆盖前一个。", 13, MUTED))

    lanes = [("进程 A", 120, GREEN), ("进程 B", 260, BLUE)]
    for name, y, color in lanes:
        body.append(text(40, y + 5, name, 13, color, 700))
        body.append(f'<line x1="120" y1="{y}" x2="1160" y2="{y}" stroke="{LINE}" stroke-width="1" stroke-dasharray="4 5"/>')

    def span(x, y, w, label, fill, stroke, color=INK, h=30):
        return [
            card(x, y - h / 2, w, h, fill=fill, stroke=stroke, r=8),
            text(x + w / 2, y + 4, label, 11.5, color, 600, anchor="middle"),
        ]

    body += span(160, 120, 150, "BEGIN IMMEDIATE", GREEN_BG, GREEN, GREEN)
    body += span(330, 120, 210, "重载：磁盘仍是 rev3", GREEN_BG, GREEN, GREEN)
    body += span(560, 120, 150, "写 a2 → rev4", GREEN_BG, GREEN, GREEN)
    body += span(730, 120, 90, "COMMIT", GREEN_BG, GREEN, GREEN)

    body += span(160, 260, 260, "BEGIN（等锁 120ms）", PAPER, LINE, MUTED)
    body += span(440, 260, 300, "拿到锁 → 重载（看到 rev4）", BLUE_BG, BLUE, BLUE)
    body += span(760, 260, 170, "写 b2 → rev5", BLUE_BG, BLUE, BLUE)
    body += span(940, 260, 90, "COMMIT", BLUE_BG, BLUE, BLUE)

    body.append(arrow(560, 145, 560, 232, MUTED, 1.2, dash="3 3"))
    body.append(text(568, 196, "A 持锁", 11, MUTED))

    body.append(card(40, 306, 1120, 96, fill=GREEN_BG, stroke="#8cc7a0"))
    body.append(text(60, 332, "结果：两条都在。", 14.5, GREEN, 700))
    body.append(
        text(60, 356, "B 没有把 A 的 rev4 冲掉 —— 因为它在改之前先读了 rev4；它只写自己脏掉的那一行。", 12.8, INK)
    )
    body.append(
        text(60, 380, "验证：tests/test_store.py —— 4 进程 × 15 条 + 15 次 merge 同一条，access_count 恰好 60，fsck 通过。", 12.3, MUTED)
    )
    return svg(1200, 420, "\n".join(body), "并发写时序")


# ---------------------------------------------------------------- bench


def bench() -> str:
    n = NUMBERS
    body = [defs()]
    body.append(text(40, 44, "写入延迟：换后端的真正理由", 20, INK, 700))
    body.append(text(40, 70, "1000 条记忆，单次 remember（含提交）的本机耗时，越短越好。", 13, MUTED))

    groups = [
        ("JSON（0.9.x）", n["json_write_p50"], n["json_write_p95"], AMBER, "#e3c07a"),
        ("SQLite（0.10）", n["sqlite_write_p50"], n["sqlite_write_p95"], GREEN, "#8cc7a0"),
    ]
    x0, x1 = 210, 720
    vmax = 56.0
    for gi, (name, p50, p95, dark, light) in enumerate(groups):
        top = 108 + gi * 96
        body.append(text(190, top + 34, name, 13, INK, 700, anchor="end"))
        for bi, (value, color, tag) in enumerate(((p50, dark, "p50"), (p95, light, "p95"))):
            y = top + bi * 34
            width = max(4, int((x1 - x0) * value / vmax))
            body.append(card(x0, y, x1 - x0, 24, fill="#eef1f4", stroke="none", r=6))
            body.append(card(x0, y, width, 24, fill=color, stroke="none", r=6))
            body.append(text(x0 - 8, y + 17, tag, 11.5, MUTED, anchor="end"))
            body.append(text(x0 + width + 10, y + 17, f"{value} ms", 12.5, INK, 700))

    # 倍率标注
    body.append(text(x0, 108 + 2 * 96 - 6, f"→ {n['json_write_p50'] / n['sqlite_write_p50']:.0f}×", 20, GREEN, 700))
    body.append(text(x0 + 74, 108 + 2 * 96 - 6, "同样的写入，代价从「重写全库」变成「改一行」", 12.5, MUTED))

    body.append(card(770, 104, 390, 176, fill=PAPER, stroke=LINE))
    body.append(text(792, 130, "代价（如实）", 13, INK, 700))
    body.append(text(792, 158, f"读库 1000 条：{n['json_load']:.0f} ms → {n['sqlite_load']:.0f} ms（基本持平）", 12.2, MUTED))
    body.append(text(792, 184, f"磁盘：{n['json_size']} MB → {n['sqlite_size']} MB", 12.2, MUTED))
    body.append(text(792, 210, "（行存储 + 4 个索引）", 11.5, MUTED))
    body.append(text(792, 242, "换到的是：写放大消失、并发安全、", 12.2, INK))
    body.append(text(792, 264, "可查询的提交历史。", 12.2, INK))

    body.append(card(40, 322, 1120, 92, fill=GREEN_BG, stroke="#8cc7a0"))
    body.append(text(60, 348, f"为什么之前是 {n['json_write_p50']:.0f}ms：", 13, GREEN, 700))
    body.append(text(60, 372, f"JSON 后端每次写入都要把整份库重新序列化并替换文件（1000 条约 {n['json_size']:.1f} MB）。", 12.8, INK))
    body.append(text(60, 396, "SQLite 只写变动的那一行 —— 改一条的代价与库的大小基本无关。", 12.8, INK))
    return svg(1200, 436, "\n".join(body), "写入延迟对比")


# ---------------------------------------------------------------- eval


def eval_chart() -> str:
    n = NUMBERS
    body = [defs()]
    body.append(text(40, 44, "组件级评测：两条通道在两类查询上的召回", 20, INK, 700))
    body.append(
        text(40, 70, "80 条记忆 / 42 条查询，两个领域。exact = 字面重叠；paraphrase = 换了个说法。k=5。", 13, MUTED)
    )

    groups = [
        ("zh-finance · exact（字面重叠）", n["lex_finance_exact"], n["hyb_finance_exact"]),
        ("zh-finance · paraphrase（换说法）", n["lex_finance_para"], n["hyb_finance_para"]),
        ("zh-tech · exact（字面重叠）", n["lex_tech_exact"], n["hyb_tech_exact"]),
        ("zh-tech · paraphrase（换说法）", n["lex_tech_para"], n["hyb_tech_para"]),
    ]
    base_y = 108
    bar_x, bar_w = 330, 660
    for index, (label, lex, hyb) in enumerate(groups):
        top = base_y + index * 82
        body.append(text(bar_x, top + 4, label, 12.3, INK, 700))
        for offset, (value, color, tag) in enumerate(((lex, AMBER, "词法"), (hyb, GREEN, "混合"))):
            y = top + 12 + offset * 30
            width = int(bar_w * value)
            body.append(card(bar_x, y, bar_w, 22, fill="#eef1f4", stroke="none", r=5))
            body.append(card(bar_x, y, max(3, width), 22, fill=color, stroke="none", r=5))
            body.append(text(bar_x + width + 10, y + 16, f"{value:.3f}", 11.8, color, 700))
            body.append(text(bar_x - 10, y + 16, tag, 11, MUTED, anchor="end"))

    body.append(card(40, 448, 1120, 96, fill=BLUE_BG, stroke="#9ec5f7"))
    body.append(text(60, 474, "读法：稠密通道的收益全部来自「换说法」（0.591 → 0.727），exact 上没有增益。", 13, INK, 700))
    body.append(
        text(60, 498, "这恰好说明它替代不了真模型：内置哈希只补字面变形。接真模型后，用同一条命令重测就知道值不值。", 12.5, MUTED)
    )
    body.append(text(60, 526, "弃答 F1 0.86 / 1.00（阈值 0.20，由 calibrate 扫出来，不是拍出来的）。", 12.5, MUTED))
    return svg(1200, 566, "\n".join(body), "组件级评测")


def write(name: str, content: str) -> None:
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)
    print(f"✓ {name}  {os.path.getsize(path) / 1024:.1f} KB")


def main() -> int:
    write("hero.svg", hero())
    write("lifecycle.svg", lifecycle())
    write("storage.svg", storage())
    write("concurrency.svg", concurrency())
    write("bench.svg", bench())
    write("eval.svg", eval_chart())
    print(f"\n输出目录：{OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
