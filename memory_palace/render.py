"""render.py - 展现层：把「记忆库现在的样子」变成人能看的东西。

为什么需要它：记忆系统的效果是**隐形的** —— 它改的是 prompt 里的一小段，用户看不见。
「用了很久」和「根本没生效」在体验上完全一样。所以必须有三层可见物：

  1. 状态行（status_line）  —— 一行字，贴在每次注入/收尾后面：取了几条、几条存疑、用了多少预算
  2. 终端视图（text_report）—— 重要性条形、未来谁先死、墓碑；不离开命令行就能看
  3. HTML 面板（html_report）—— 自包含单文件，含衰减曲线：能亲眼看到「遗忘」在未来 60 天发生

HTML 不依赖任何 CDN / 图片 / 网络，双击即开；配色跟随系统深浅色。
"""
from __future__ import annotations

import html
import math
from typing import Any, Iterable

from .core import DAY, MemoryNode

TIER_LABEL = {
    "core": "核心",
    "fact": "事实",
    "event": "经历",
    "ephemeral": "临时",
    "trace": "碎片",
}
TIER_COLOR = {
    "core": "#b3462f",
    "fact": "#2f6f5e",
    "event": "#4a6fa5",
    "ephemeral": "#8a8a80",
    "trace": "#a08a5a",
}


# ---------------------------------------------------------------- 公共读数


def _importance(palace, now: float) -> dict[str, dict[str, float]]:
    return palace.value.assess(palace.graph, now, palace.logic.support_map())


def _days_to_floor(node: MemoryNode, now: float, floor: float, gain: float) -> float | None:
    """按当前稳定性估算「还有几天跌破遗忘线」。钉死的返回 None（永不）。"""
    if node.pinned:
        return None
    weight = node.effective_weight(now, gain)
    if weight <= floor:
        return 0.0
    stability = node.stability(now, gain)
    if stability <= 0:
        return None
    days = stability * math.log(weight / floor)
    return round(max(0.0, days), 1) if math.isfinite(days) else None


def life_label(days: float | None) -> str:
    """把「还能撑几天」翻成人话。身份类记忆的稳定性是 1e6 量级，直出会是「13659284 天」。"""
    if days is None:
        return "永不衰减"
    if days >= 365 * 5:
        return "极稳（>5年）"
    if days <= 0:
        return "已过期"
    return f"{days:.0f}天"


def decay_series(node: MemoryNode, now: float, horizon_days: float, steps: int = 20) -> list[float]:
    gain = node.AFFECT_GAIN
    return [
        node.effective_weight(now + horizon_days * i / (steps - 1), gain)
        for i in range(steps)
    ]


def status_line(ctx: dict[str, Any], rev: int | None = None) -> str:
    """一行状态：让用户知道「这次到底用了几条记忆」。"""
    items = ctx.get("items") or []
    pinned = sum(1 for i in items if i.get("pinned"))
    shaky = sum(1 for i in items if i.get("fidelity", 1.0) < 0.7)
    parts = [
        f"记忆 {len(items)}/{ctx.get('total_candidates', 0)}",
        f"钉死 {pinned}" if pinned else "",
        f"存疑 {shaky}" if shaky else "",
        f"{ctx.get('used', 0)}/{ctx.get('budget', 0)}字",
    ]
    if rev:
        parts.append(f"自述 rev{rev}")
    return "[" + " · ".join(p for p in parts if p) + "]"


# ---------------------------------------------------------------- 终端视图


def _bar(value: float, scale: float, width: int = 14) -> str:
    filled = 0 if scale <= 0 else int(round(max(0.0, min(1.0, value / scale)) * width))
    return "█" * filled + "·" * (width - filled)


def text_report(palace, limit: int = 10, horizon_days: float = 30) -> str:
    now = palace.now()
    cfg = palace._config
    snap = palace.snapshot()
    assessment = _importance(palace, now)
    nodes = sorted(palace.graph.all_nodes(), key=lambda n: -assessment.get(n.name, {}).get("total", 0.0))
    scale = max((assessment.get(n.name, {}).get("total", 0.0) for n in nodes), default=1.0) or 1.0

    lines = [
        f"记忆宫殿 · {snap['nodes']} 节点 / {snap['edges']} 边 / 墓碑 {snap['tombstone']}"
        f" / 事件 {snap['events']}",
        f"价值 {snap['entropy_health']}（top1 占 {snap['attention']['top_share']:.0%}）"
        f"｜结构 {snap['structure_health']}（孤立 {snap['structure']['isolated_share']:.0%}）",
        "",
    ]
    if not nodes:
        lines.append("（空库：还没有任何记忆）")
        return "\n".join(lines)

    lines.append(f"{'重要性':<16} {'寿命':<7} 记忆")
    for node in nodes[:limit]:
        parts = assessment.get(node.name, {})
        bar = _bar(parts.get("total", 0.0), scale)
        days = _days_to_floor(node, now, cfg.forget_w, cfg.affect_stability_gain)
        life = "钉死" if node.pinned else life_label(days)
        mark = "!" if node.affect.fidelity(cfg.affect_distortion) < cfg.narrative_reliability_warn else " "
        tag = node.tags[0] if node.tags else TIER_LABEL.get(node.tier, node.tier)
        text = node.content if len(node.content) <= 26 else node.content[:25] + "…"
        lines.append(f"{bar} {parts.get('total', 0.0):4.2f} {life:<6}{mark} [{tag}] {text}")

    if len(nodes) > limit:
        lines.append(f"… 另有 {len(nodes) - limit} 条")

    soon = [
        (n, _days_to_floor(n, now, cfg.forget_w, cfg.affect_stability_gain))
        for n in nodes
    ]
    dying = sorted([(d, n) for n, d in soon if d is not None and 0 < d <= horizon_days])
    lines.append("")
    if dying:
        names = "、".join(n.name for _d, n in dying[:4])
        lines.append(f"{horizon_days:.0f} 天内将跌破遗忘线：{len(dying)} 条（{names}）")
    else:
        lines.append(f"{horizon_days:.0f} 天内没有记忆会跌破遗忘线")

    styles = palace.active_style()
    lines.append("生效风格：" + ("；".join(styles) if styles else "（无）"))
    head = palace.narrative_head()
    if head:
        arcs = " → ".join(f"{c['arc']}「{c['title']}」" for c in head["chapters"])
        lines.append(f"自述 rev{head['rev']}：{arcs}｜单主题 {head['monopoly']:.2f}")
    if palace.tombstone:
        lines.append(f"已遗忘：{'、'.join(sorted(palace.tombstone)[:6])}")
    return "\n".join(lines)


# ---------------------------------------------------------------- HTML 面板


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _decay_svg(palace, nodes: list[MemoryNode], now: float, horizon: float, width: int = 760, height: int = 240) -> str:
    pad_l, pad_r, pad_t, pad_b = 44, 16, 16, 28
    inner_w, inner_h = width - pad_l - pad_r, height - pad_t - pad_b
    series = [(n, decay_series(n, now, horizon)) for n in nodes]
    peak = max((s[0] for _n, s in series), default=1.0) or 1.0

    def x(i: int, steps: int) -> float:
        return pad_l + inner_w * i / (steps - 1)

    def y(v: float) -> float:
        return pad_t + inner_h * (1 - min(1.0, v / peak))

    parts = [
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="未来 {horizon:.0f} 天的衰减曲线" class="decay">',
        f'<line x1="{pad_l}" y1="{pad_t}" x2="{pad_l}" y2="{height - pad_b}" class="axis"/>',
        f'<line x1="{pad_l}" y1="{height - pad_b}" x2="{width - pad_r}" y2="{height - pad_b}" class="axis"/>',
    ]
    for frac in (0.25, 0.5, 0.75, 1.0):
        yy = y(peak * frac)
        parts.append(f'<line x1="{pad_l}" y1="{yy:.1f}" x2="{width - pad_r}" y2="{yy:.1f}" class="grid"/>')
        parts.append(f'<text x="{pad_l - 8}" y="{yy + 4:.1f}" class="tick" text-anchor="end">{peak * frac:.2f}</text>')
    for label, frac in (("现在", 0.0), (f"+{horizon / 2:.0f}天", 0.5), (f"+{horizon:.0f}天", 1.0)):
        parts.append(f'<text x="{pad_l + inner_w * frac:.1f}" y="{height - 8}" class="tick" text-anchor="middle">{label}</text>')

    for node, values in series:
        pts = " ".join(f"{x(i, len(values)):.1f},{y(v):.1f}" for i, v in enumerate(values))
        color = TIER_COLOR.get(node.tier, "#6b6b66")
        dash = ' stroke-dasharray="4 3"' if node.pinned or node.tier == "trace" else ""
        parts.append(
            f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="2"{dash} opacity="0.9"/>'
        )
    parts.append("</svg>")
    legend = "".join(
        f'<span class="lg"><i style="background:{TIER_COLOR.get(n.tier, "#6b6b66")}"></i>{_esc(_short(n.content, 10))}</span>'
        for n in nodes
    )
    return "".join(parts) + f'<div class="legend">{legend}</div>'


def _short(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def html_report(palace, title: str = "记忆宫殿", horizon_days: float = 60) -> str:
    now = palace.now()
    cfg = palace._config
    snap = palace.snapshot()
    assessment = _importance(palace, now)
    nodes = sorted(palace.graph.all_nodes(), key=lambda n: -assessment.get(n.name, {}).get("total", 0.0))
    top = nodes[:8]
    total_mass = sum(max(0.0, v["total"]) for v in assessment.values()) or 1.0
    head = palace.narrative_head()

    cards = []
    for node in nodes:
        parts = assessment.get(node.name, {})
        fidelity = node.affect.fidelity(cfg.affect_distortion)
        days = _days_to_floor(node, now, cfg.forget_w, cfg.affect_stability_gain)
        if node.pinned:
            life = "钉死 · 永不衰减"
        elif days is None:
            life = "永不衰减"
        elif days <= 0:
            life = "已跌破遗忘线"
        elif days >= 365 * 5:
            life = "极稳（>5年）"
        else:
            life = f"预计 {days:.0f} 天后跌破线"
        badges = []
        if node.pinned:
            badges.append('<span class="badge pin">钉死</span>')
        if node.consolidated_from:
            badges.append(f'<span class="badge">沉淀 {len(node.consolidated_from)} 条</span>')
        if fidelity < cfg.narrative_reliability_warn:
            badges.append(f'<span class="badge warn">可能失真 {fidelity:.2f}</span>')
        affect = ""
        if node.affect.source != "neutral":
            affect = (
                f'<div class="affect">情绪 v{node.affect.valence:+.2f} / a{node.affect.arousal:.2f}'
                f'｜来源 {_esc(node.affect.source)}</div>'
            )
        score = parts.get("total", 0.0)
        pct = max(2.0, min(100.0, score / max(1e-9, max((a["total"] for a in assessment.values()), default=1.0)) * 100))
        cards.append(
            f'''<article class="card t-{_esc(node.tier)}">
  <header><span class="tier">{_esc(TIER_LABEL.get(node.tier, node.tier))}</span>
  <span class="tags">{_esc(" · ".join(node.tags) or "无标签")}</span></header>
  <p class="content">{_esc(node.content)}</p>
  <div class="meter"><div style="width:{pct:.1f}%"></div></div>
  <footer><span>重要度 {score:.2f}</span><span>{_esc(life)}</span>
  <span class="fidelity">可靠 {fidelity:.2f}</span></footer>
  {affect}<div class="badges">{"".join(badges)}</div>
</article>'''
        )

    shares = sorted(
        ((n.name, assessment.get(n.name, {}).get("total", 0.0) / total_mass) for n in nodes),
        key=lambda x: -x[1],
    )[:8]
    share_rows = "".join(
        f'<div class="row"><span class="name">{_esc(name)}</span>'
        f'<div class="track"><div style="width:{share * 100:.1f}%"></div></div>'
        f'<span class="pct">{share:.0%}</span></div>'
        for name, share in shares
    )

    chapters = ""
    if head:
        chapters = "".join(
            f'<li><span class="arc">{_esc(c["arc"])}</span><b>{_esc(c["title"])}</b>'
            f'<span class="n">{len(c["nodes"])} 条</span><span class="share">{c["share"]:.0%}</span></li>'
            for c in head["chapters"]
        )

    graveyard = "".join(
        f'<li><b>{_esc(name)}</b><span>{_esc(_short(node.content, 40))}</span></li>'
        for name, node in sorted(palace.tombstone.items())[:12]
    ) or '<li class="empty">还没有真正忘掉任何东西</li>'

    styles = "".join(f'<span class="chip">{_esc(s)}</span>' for s in palace.active_style()) or '<span class="muted">暂无（证据不足）</span>'
    dying = [n for n in nodes if (_days_to_floor(n, now, cfg.forget_w, cfg.affect_stability_gain) or 1e9) <= 30]

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(title)}</title>
<style>
:root{{--bg:#f6f6f3;--card:#fff;--ink:#1d1d1b;--muted:#6d6d66;--line:#e3e2dc;--accent:#2f6f5e;--warn:#b3462f}}
@media (prefers-color-scheme:dark){{:root{{--bg:#16171a;--card:#1e2024;--ink:#eceae4;--muted:#9b9b93;--line:#2e3138;--accent:#6fbfa6;--warn:#e0705b}}}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);
 font:15px/1.6 system-ui,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}}
.wrap{{max-width:1000px;margin:0 auto;padding:28px 20px 64px}}
h1{{font-size:22px;margin:0 0 4px;letter-spacing:.5px}}
.sub{{color:var(--muted);font-size:13px;margin-bottom:22px}}
.stats{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:26px}}
.stat{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}}
.stat b{{display:block;font-size:20px;line-height:1.2}}
.stat span{{color:var(--muted);font-size:12px}}
h2{{font-size:15px;margin:30px 0 12px;letter-spacing:.4px}}
h2 small{{color:var(--muted);font-weight:400;letter-spacing:0;margin-left:8px;font-size:12px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:12px}}
.card{{background:var(--card);border:1px solid var(--line);border-left:3px solid var(--line);
 border-radius:10px;padding:12px 14px}}
.card.t-core{{border-left-color:#b3462f}}.card.t-fact{{border-left-color:#2f6f5e}}
.card.t-event{{border-left-color:#4a6fa5}}.card.t-ephemeral{{border-left-color:#8a8a80}}
.card.t-trace{{border-left-color:#a08a5a}}
.card header{{display:flex;justify-content:space-between;gap:8px;font-size:11px;color:var(--muted)}}
.tier{{font-weight:600;letter-spacing:.5px}}
.content{{margin:8px 0 10px;font-size:14px;line-height:1.5;word-break:break-word}}
.meter{{height:4px;background:var(--line);border-radius:2px;overflow:hidden}}
.meter>div{{height:100%;background:var(--accent)}}
.card footer{{display:flex;justify-content:space-between;gap:6px;font-size:11px;color:var(--muted);margin-top:7px}}
.badges{{margin-top:6px;display:flex;gap:6px;flex-wrap:wrap}}
.badge{{font-size:11px;border:1px solid var(--line);border-radius:999px;padding:1px 8px;color:var(--muted)}}
.badge.pin{{border-color:var(--warn);color:var(--warn)}}
.badge.warn{{border-color:var(--warn);color:var(--warn)}}
.affect{{font-size:11px;color:var(--muted);margin-top:6px}}
.panel{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}}
svg.decay{{width:100%;height:auto;display:block}}
.axis{{stroke:var(--line);stroke-width:1}}
.grid{{stroke:var(--line);stroke-width:.6;stroke-dasharray:2 4;opacity:.7}}
.tick{{fill:var(--muted);font-size:10px}}
.legend{{display:flex;flex-wrap:wrap;gap:10px;margin-top:8px;font-size:11px;color:var(--muted)}}
.lg i{{display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:4px}}
.row{{display:flex;align-items:center;gap:10px;margin:6px 0;font-size:12px}}
.row .name{{width:130px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--muted)}}
.row .track{{flex:1;height:6px;background:var(--line);border-radius:3px;overflow:hidden}}
.row .track>div{{height:100%;background:var(--accent)}}
.row .pct{{width:38px;text-align:right;color:var(--muted)}}
ol.chapters{{list-style:none;padding:0;margin:0}}
ol.chapters li{{display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px dashed var(--line);font-size:13px}}
ol.chapters li:last-child{{border-bottom:0}}
.arc{{font-size:11px;color:var(--accent);border:1px solid var(--accent);border-radius:4px;padding:0 5px}}
ol.chapters .n{{color:var(--muted);font-size:12px}}
ol.chapters .share{{margin-left:auto;color:var(--muted);font-size:12px}}
ul.grave{{list-style:none;padding:0;margin:0;font-size:12px;color:var(--muted)}}
ul.grave li{{display:flex;gap:10px;padding:5px 0;border-bottom:1px dashed var(--line)}}
ul.grave li b{{color:var(--ink);font-weight:500;min-width:110px}}
.chip{{display:inline-block;border:1px solid var(--line);border-radius:999px;padding:2px 10px;font-size:12px;margin:0 6px 6px 0}}
.muted{{color:var(--muted)}}.empty{{color:var(--muted)}}
.note{{margin-top:26px;color:var(--muted);font-size:12px;line-height:1.7}}
</style></head><body><div class="wrap">
<h1>{_esc(title)}</h1>
<div class="sub">生成于 {_esc(_when(now))}｜{_esc(snap["nodes"])} 条记忆、{_esc(snap["edges"])} 条关联、{_esc(snap["tombstone"])} 条已遗忘</div>

<div class="stats">
  <div class="stat"><b>{_esc(snap["nodes"])}</b><span>在册记忆</span></div>
  <div class="stat"><b>{snap["entropy_health"]}</b><span>价值分布（top1 {snap["attention"]["top_share"]:.0%}）</span></div>
  <div class="stat"><b>{snap["structure_health"]}</b><span>结构（孤立 {snap["structure"]["isolated_share"]:.0%}）</span></div>
  <div class="stat"><b>{len(dying)}</b><span>30 天内将跌破遗忘线</span></div>
  <div class="stat"><b>{_esc(snap["events"])}</b><span>累计事件</span></div>
</div>

<h2>记忆卡片<small>按重要度排序；条形是重要度，不是相似度</small></h2>
<div class="grid">{"".join(cards) or '<p class="muted">还没有记忆</p>'}</div>

<h2>衰减曲线<small>未来 {horizon_days:.0f} 天，每条记忆的有效强度（虚线 = 钉死/碎片）</small></h2>
<div class="panel">{_decay_svg(palace, top, now, horizon_days)}</div>

<h2>注意力份额<small>谁在占据「我在乎的东西」</small></h2>
<div class="panel">{share_rows or '<p class="muted">无</p>'}</div>

<h2>自述<small>{"第 %d 版" % head["rev"] if head else "还没讲过"}</small></h2>
<div class="panel">{f'<ol class="chapters">{chapters}</ol>' if chapters else '<p class="muted">还没有形成自述</p>'}</div>

<h2>生效风格</h2>
<div class="panel">{styles}</div>

<h2>墓碑<small>被主动放下的东西，仍可溯源</small></h2>
<div class="panel"><ul class="grave">{graveyard}</ul></div>

<p class="note">
这不是仪表盘，是一张「记忆的体检报告」。三条读法：<br>
① 卡片条形很长但寿命很短 = 现在很亮，但很快会被忘掉 —— 如果不重要，这是对的。<br>
② 衰减曲线里最先触底的，就是系统认为「最该放手」的；钉死的线是平的，它永远不会掉下去。<br>
③ 墓碑不是失败，是取舍。全部保留等于没有取舍。
</p>
</div></body></html>"""


def _when(ts: float) -> str:
    import datetime

    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
