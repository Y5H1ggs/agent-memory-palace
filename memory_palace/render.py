"""render.py - 展现层：把「记忆库现在的样子」变成人能看的东西。

三层可见物：
  1. 状态行（status_line）  —— 一行字：取了几条、几条存疑、用了多少预算
  2. 终端视图（text_report）—— 重要性条形、剩余寿命、遗忘预警、墓碑
  3. HTML 面板（html_report）—— 单文件自包含：衰减曲线、活动时间轴、卡片、自述、墓碑

HTML 的设计原则（第一版被用户指出「看着怪」，改的就是这几条）：
  · 图表必须有信息：纵轴是**有效强度**不是排名，遗忘线画出来，曲线要能**跨过**它；
    所有线挤在顶部说明这张图没在讲任何事。
  · 卡片必须齐：两列等高，重复的信息（可靠度 1.00）不印，徽章进头部。
  · 数字必须说人话：flat / isolated 这类内部枚举要翻成「无梯度 / 全是孤岛」。
"""
from __future__ import annotations

import html
import math
from typing import Any

from .core import DAY, MemoryNode

TIER_LABEL = {"core": "核心", "fact": "事实", "event": "经历", "ephemeral": "临时", "trace": "碎片"}
TIER_COLOR = {"core": "#b3462f", "fact": "#2f6f5e", "event": "#4a6fa5", "ephemeral": "#8a8a80", "trace": "#a08a5a"}
HEALTH_ZH = {
    "healthy": "有梯度", "flat": "无梯度", "narrow": "过窄",
    "connected": "连通", "isolated": "全是孤岛", "fragmented": "碎裂", "hub": "单枢纽", "empty": "空",
}
EVENT_ZH = {
    "recall": "检索", "gc": "遗忘", "consolidate": "沉淀", "crystallize": "风格固化",
    "decrystallize": "风格撤销", "reflect": "反思", "restore": "恢复", "grasp": "抓取",
    "appraise": "情绪评价", "feel_outcome": "结果反馈", "selfcorrect_kept": "自修正",
    "selfcorrect_reverted": "修正回滚", "memory_demote": "降级", "pin": "钉死", "drop": "丢弃",
}


# ---------------------------------------------------------------- 基础读数


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def life_label(days: float | None) -> str:
    if days is None:
        return "永不衰减"
    if days >= 365 * 5:
        return "极稳（>5年）"
    if days <= 0:
        return "已过期"
    return f"{days:.0f}天"


def _days_to_floor(node: MemoryNode, now: float, floor: float, gain: float) -> float | None:
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


def decay_series(node: MemoryNode, now: float, horizon_days: float, steps: int = 21) -> list[float]:
    gain = node.AFFECT_GAIN
    return [node.effective_weight(now + horizon_days * i / (steps - 1), gain) for i in range(steps)]


def status_line(ctx: dict[str, Any], rev: int | None = None) -> str:
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


def _bar(value: float, scale: float, width: int = 12) -> str:
    filled = 0 if scale <= 0 else int(round(max(0.0, min(1.0, value / scale)) * width))
    return "█" * filled + "·" * (width - filled)


def text_report(palace, limit: int = 10, horizon_days: float = 30) -> str:
    now = palace.now()
    cfg = palace._config
    snap = palace.snapshot()
    assessment = palace.assessment(now)
    nodes = sorted(palace.graph.all_nodes(), key=lambda n: -assessment.get(n.name, {}).get("total", 0.0))
    scale = max((assessment.get(n.name, {}).get("total", 0.0) for n in nodes), default=1.0) or 1.0

    lines = [
        f"记忆宫殿 · {snap['nodes']} 节点 / {snap['edges']} 边 / 墓碑 {snap['tombstone']}"
        f" / 事件 {snap['events']}",
        f"价值 {HEALTH_ZH.get(snap['entropy_health'], snap['entropy_health'])}"
        f"（top1 占 {snap['attention']['top_share']:.0%}）"
        f"｜结构 {HEALTH_ZH.get(snap['structure_health'], snap['structure_health'])}"
        f"（孤立 {snap['structure']['isolated_share']:.0%}）",
        "",
    ]
    if not nodes:
        lines.append("（空库：还没有任何记忆）")
        return "\n".join(lines)

    lines.append(f"{'重要性':<14} {'寿命':<8} 记忆")
    for node in nodes[:limit]:
        parts = assessment.get(node.name, {})
        bar = _bar(parts.get("total", 0.0), scale)
        days = _days_to_floor(node, now, cfg.forget_w, cfg.affect_stability_gain)
        life = "钉死" if node.pinned else life_label(days)
        mark = "!" if node.affect.fidelity(cfg.affect_distortion) < cfg.narrative_reliability_warn else " "
        tag = node.tags[0] if node.tags else TIER_LABEL.get(node.tier, node.tier)
        text = node.content if len(node.content) <= 24 else node.content[:23] + "…"
        lines.append(f"{bar} {parts.get('total', 0.0):4.2f} {life:<7}{mark} [{tag}] {text}")

    if len(nodes) > limit:
        lines.append(f"… 另有 {len(nodes) - limit} 条")

    dying = sorted(
        (d, n) for n in nodes
        if (d := _days_to_floor(n, now, cfg.forget_w, cfg.affect_stability_gain)) is not None
        and 0 < d <= horizon_days
    )
    lines.append("")
    if dying:
        lines.append(f"{horizon_days:.0f} 天内将跌破遗忘线：{len(dying)} 条"
                     f"（{'、'.join(n.name for _d, n in dying[:4])}）")
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


# ---------------------------------------------------------------- SVG


LIFESPAN_BUCKETS = (
    (0, 7, "< 7 天", "#b3462f"),
    (7, 30, "7–30 天", "#c9764f"),
    (30, 90, "1–3 个月", "#c9a227"),
    (90, 365, "3–12 个月", "#5c8a6f"),
    (365, 365 * 5, "> 1 年", "#2f6f5e"),
)
PINNED_COLOR = "#1c1c1a"


def _lifespan_svg(palace, nodes: list[MemoryNode], now: float, width: int = 820, height: int = 200) -> str:
    """按「还能撑多久」分桶。

    为什么不画连续衰减曲线：大多数节点的稳定性以「月」计，30 天内几乎是水平线，
    七条线挤在顶部 —— 那张图什么也没说。分桶永远可读，而且直接回答「我该担心哪几条」。
    """
    cfg = palace._config
    pinned = sum(1 for n in nodes if n.pinned)
    counts = [0] * len(LIFESPAN_BUCKETS)
    for node in nodes:
        if node.pinned:
            continue
        days = _days_to_floor(node, now, cfg.forget_w, cfg.affect_stability_gain)
        days = 0.0 if days is None else days
        for i, (low, high, _label, _color) in enumerate(LIFESPAN_BUCKETS):
            if low <= days < high:
                counts[i] += 1
                break
        else:
            counts[-1] += 1
    if not nodes or (not any(counts) and not pinned):
        return '<p class="muted">没有可画的记忆</p>'

    pad_l, pad_b, pad_t, pad_r = 34, 46, 14, 16
    inner_w, inner_h = width - pad_l - pad_r, height - pad_t - pad_b
    total = sum(counts) + pinned
    peak = max(counts + [1])
    slot = inner_w / (len(counts) + 1)
    bar_w = slot * 0.46

    def bar(i: int, value: int, color: str, label: str) -> list[str]:
        h = inner_h * value / peak
        x = pad_l + i * slot + (slot - bar_w) / 2
        out = [
            f'<rect x="{x:.1f}" y="{pad_t + inner_h - h:.1f}" width="{bar_w:.1f}" height="{h:.1f}"'
            f' rx="3" fill="{color}" opacity="0.92"/>',
            f'<text x="{x + bar_w / 2:.1f}" y="{pad_t + inner_h - h - 6:.1f}" class="bar-value">{value}</text>',
            f'<text x="{x + bar_w / 2:.1f}" y="{height - 26}" class="tick mid">{label}</text>',
            f'<text x="{x + bar_w / 2:.1f}" y="{height - 10}" class="tick mid">'
            f'{value / max(1, total) * 100:.0f}%</text>',
        ]
        return out

    parts = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="记忆剩余寿命分布">']
    parts.append(f'<line x1="{pad_l}" y1="{pad_t + inner_h}" x2="{width - pad_r}" y2="{pad_t + inner_h}" class="axis"/>')
    for i, (count, (_low, _high, label, color)) in enumerate(zip(counts, LIFESPAN_BUCKETS)):
        parts.extend(bar(i, count, color, label))
    parts.extend(bar(len(counts), pinned, PINNED_COLOR, "钉死"))
    parts.append("</svg>")
    urgent = counts[0] + counts[1]
    note = (
        f'未来 30 天内会掉 {urgent} 条（占 {urgent / max(1, total):.0%}）；'
        f'钉死的 {pinned} 条永远不会掉。'
    )
    return "".join(parts) + f'<p class="chart-note">{note}</p>'


def _flow_svg(palace, nodes: list[MemoryNode], now: float, weeks: int = 12,
              width: int = 820, height: int = 190) -> str:
    """每周「新增 vs 遗忘」。记忆不是只进不出的池子 —— 这张图就是证据。"""
    new_counts = [0] * weeks
    dropped = [0] * weeks
    span = weeks * 7 * DAY
    # 新增要算上已经死掉的：墓碑里的节点当年也是写进来的，只统计活着的会把「进」画少。
    for node in list(nodes) + list(palace.tombstone.values()):
        age = now - node.created_at
        if 0 <= age <= span:
            index = weeks - 1 - int(age / (7 * DAY))
            if 0 <= index < weeks:
                new_counts[index] += 1
    for event in palace.events:
        if event.kind not in ("gc", "drop"):
            continue
        age = now - event.ts
        if not (0 <= age <= span):
            continue
        index = weeks - 1 - int(age / (7 * DAY))
        if 0 <= index < weeks:
            dropped[index] += len(event.payload.get("deleted", []) or [event.payload.get("name")]) if event.kind == "gc" \
                else 1
    if not any(new_counts) and not any(dropped):
        return '<p class="muted">还没有活动记录</p>'

    pad_l, pad_b, pad_t, pad_r = 34, 40, 14, 16
    inner_w, inner_h = width - pad_l - pad_r, height - pad_t - pad_b
    peak = max(new_counts + dropped + [1])
    base = pad_t + inner_h
    slot = inner_w / weeks
    bar_w = slot * 0.30

    parts = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="每周新增与遗忘">']
    parts.append(f'<line x1="{pad_l}" y1="{base}" x2="{width - pad_r}" y2="{base}" class="axis"/>')
    for i in range(weeks):
        left = pad_l + i * slot + slot / 2 - bar_w - 1
        for offset, value, color in ((0, new_counts[i], "#2f6f5e"), (bar_w + 2, dropped[i], "#b3462f")):
            if not value:
                continue
            h = inner_h * value / peak
            parts.append(
                f'<rect x="{left + offset:.1f}" y="{base - h:.1f}" width="{bar_w:.1f}"'
                f' height="{h:.1f}" rx="2" fill="{color}" opacity="0.9"/>'
            )
        if i % 3 == 0:
            parts.append(
                f'<text x="{pad_l + i * slot + slot / 2:.1f}" y="{height - 10}" class="tick mid">'
                f'-{weeks - i}w</text>'
            )
    parts.append(f'<text x="6" y="{pad_t + 10}" class="tick">{peak}</text>')
    parts.append(f'<text x="6" y="{base}" class="tick">0</text>')
    parts.append("</svg>")
    legend = (
        '<div class="legend"><span class="lg"><i style="background:#2f6f5e"></i>新增</span>'
        '<span class="lg"><i style="background:#b3462f"></i>遗忘</span></div>'
    )
    return "".join(parts) + legend


def _short(text: str, n: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


# ---------------------------------------------------------------- HTML 面板


def html_report(palace, title: str = "记忆宫殿", horizon_days: float = 30) -> str:
    now = palace.now()
    cfg = palace._config
    snap = palace.snapshot()
    assessment = palace.assessment(now)
    nodes = sorted(palace.graph.all_nodes(), key=lambda n: -assessment.get(n.name, {}).get("total", 0.0))
    top = nodes[:7]
    total_mass = sum(max(0.0, v["total"]) for v in assessment.values()) or 1.0
    head = palace.narrative_head()
    dying = [n for n in nodes
             if (_days_to_floor(n, now, cfg.forget_w, cfg.affect_stability_gain) or 1e9) <= 30]
    pinned = [n for n in nodes if n.pinned]
    avg_life = [d for n in nodes
                if (d := _days_to_floor(n, now, cfg.forget_w, cfg.affect_stability_gain)) is not None]

    summary = (
        f"{snap['nodes']} 条记忆里 {len(pinned)} 条永不衰减，"
        f"{len(dying)} 条会在一个月内跌破遗忘线，"
        f"{snap['tombstone']} 条已经被主动放下。"
    )

    cards = []
    for node in nodes:
        parts = assessment.get(node.name, {})
        fidelity = node.affect.fidelity(cfg.affect_distortion)
        days = _days_to_floor(node, now, cfg.forget_w, cfg.affect_stability_gain)
        if node.pinned:
            life, life_cls = "钉死 · 永不衰减", "pin"
        elif days is None:
            life, life_cls = "永不衰减", "steady"
        elif days <= 0:
            life, life_cls = "已跌破遗忘线", "warn"
        elif days >= 365 * 5:
            life, life_cls = "极稳（>5 年）", "steady"
        else:
            life, life_cls = f"{days:.0f} 天后跌破线", "warn" if days <= 30 else "steady"
        score = parts.get("total", 0.0)
        pct = max(3.0, min(100.0, score / max(1e-9, max((a["total"] for a in assessment.values()), default=1.0)) * 100))
        badges = []
        # 「钉死」已经在寿命位写着了，这里再挂一枚徽章只是噪音
        if node.consolidated_from:
            badges.append(f'<span class="badge">{len(node.consolidated_from)} 条沉淀而来</span>')
        if fidelity < cfg.narrative_reliability_warn:
            badges.append(f'<span class="badge warn">可能失真 · {fidelity:.2f}</span>')
        affect = ""
        strong = abs(node.affect.valence) >= 0.5 or node.affect.arousal >= 0.6
        if node.affect.source != "neutral" and strong:
            tone = "负" if node.affect.valence < 0 else "正"
            affect = (
                f'<div class="affect"><span class="dot" style="background:'
                f'{TIER_COLOR.get(node.tier)}"></span>情绪 {tone} {abs(node.affect.valence):.2f}'
                f' · 唤醒 {node.affect.arousal:.2f} · {_esc(node.affect.source)}</div>'
            )
        cards.append(
            f'''<article class="card">
  <div class="card-top">
    <span class="tier" style="color:{TIER_COLOR.get(node.tier, "#6b6b66")}">{_esc(TIER_LABEL.get(node.tier, node.tier))}</span>
    <span class="tags">{_esc(" · ".join(node.tags) or "无标签")}</span>
    <span class="score">{score:.2f}</span>
  </div>
  <p class="content">{_esc(node.content)}</p>
  <div class="meter"><div style="width:{pct:.1f}%;background:{TIER_COLOR.get(node.tier, "#6b6b66")}"></div></div>
  <div class="card-foot"><span class="life {life_cls}">{_esc(life)}</span>
  {f'<span class="fidelity">可靠度 {fidelity:.2f}</span>' if fidelity < 0.95 else ''}</div>
  {affect}
  <div class="badges">{"".join(badges)}</div>
</article>'''
        )

    shares = sorted(((n.name, assessment.get(n.name, {}).get("total", 0.0) / total_mass) for n in nodes),
                    key=lambda x: -x[1])[:8]
    share_rows = "".join(
        f'<div class="row"><span class="name">{_esc(name)}</span>'
        f'<span class="track"><span style="width:{share * 100:.1f}%"></span></span>'
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
    narrative_meta = (
        f'第 {head["rev"]} 版 · 可靠度 {head["reliability"]:.2f} · 单主题占比 {head["monopoly"]:.2f}'
        if head else "还没有讲过"
    )

    graveyard = "".join(
        f'<li><b>{_esc(name)}</b><span>{_esc(_short(node.content, 46))}</span></li>'
        for name, node in sorted(palace.tombstone.items())[:10]
    ) or '<li class="empty">还没有真正忘掉任何东西</li>'

    styles = "".join(f'<span class="chip">{_esc(s)}</span>' for s in palace.active_style()) or \
        '<span class="muted">暂无（证据不足或存在对立）</span>'

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(title)}</title>
<style>
:root{{--bg:#f7f6f3;--card:#fff;--ink:#1c1c1a;--muted:#71716a;--line:#e6e4dd;
 --accent:#2f6f5e;--warn:#b3462f;--shadow:0 1px 2px rgba(28,28,26,.04),0 8px 24px rgba(28,28,26,.05)}}
@media (prefers-color-scheme:dark){{:root{{--bg:#141518;--card:#1c1e22;--ink:#ecebe5;--muted:#9a9a92;
 --line:#2b2e34;--accent:#6fbfa6;--warn:#e0705b;--shadow:none}}}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);
 font:14px/1.65 system-ui,-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
 -webkit-font-smoothing:antialiased}}
.wrap{{max-width:1080px;margin:0 auto;padding:36px 24px 72px}}
.hero{{margin-bottom:26px}}
.hero h1{{font-size:26px;margin:0 0 6px;letter-spacing:.3px;font-weight:650}}
.hero p{{margin:0;color:var(--muted);font-size:13px}}
.chips{{margin-top:12px;display:flex;gap:8px;flex-wrap:wrap}}
.chip-h{{font-size:12px;border:1px solid var(--line);background:var(--card);border-radius:999px;
 padding:3px 11px;color:var(--muted)}}
.chip-h b{{color:var(--ink);font-weight:600}}
.kpis{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin:22px 0 30px}}
.kpi{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;box-shadow:var(--shadow)}}
.kpi .v{{font-size:23px;font-weight:650;line-height:1.15;font-variant-numeric:tabular-nums}}
.kpi .l{{color:var(--muted);font-size:12px;margin-top:2px}}
section{{margin:0 0 30px}}
h2{{font-size:15px;margin:0 0 4px;letter-spacing:.2px}}
h2 small{{font-weight:400;color:var(--muted);font-size:12px;margin-left:8px}}
.panel{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px;
 box-shadow:var(--shadow);margin-top:10px}}
svg.chart{{width:100%;height:auto;display:block}}
.axis{{stroke:var(--line);stroke-width:1}}
.bar-value{{fill:var(--ink);font-size:11px;font-weight:600;text-anchor:middle;font-variant-numeric:tabular-nums}}
.chart-note{{margin:10px 0 0;font-size:12px;color:var(--muted)}}
.grid{{stroke:var(--line);stroke-width:.7;stroke-dasharray:2 5}}
.floor{{stroke:var(--warn);stroke-width:1;stroke-dasharray:5 4;opacity:.75}}
.floor-label{{fill:var(--warn);font-size:9.5px}}
.tick{{fill:var(--muted);font-size:10px;font-variant-numeric:tabular-nums}}
.tick.end{{text-anchor:end}}.tick.mid{{text-anchor:middle}}
.series-label{{fill:var(--muted);font-size:10px}}
.legend{{display:flex;flex-wrap:wrap;gap:14px;margin-top:10px;font-size:11.5px;color:var(--muted)}}
.lg i{{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:5px;vertical-align:-1px}}
.grid2{{display:grid;grid-template-columns:1.35fr 1fr;gap:14px}}
@media (max-width:820px){{.grid2{{grid-template-columns:1fr}}}}
.cards{{display:grid;grid-template-columns:repeat(2,1fr);gap:12px}}
@media (max-width:820px){{.cards{{grid-template-columns:1fr}}}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:13px 15px;
 box-shadow:var(--shadow);display:flex;flex-direction:column;min-height:132px}}
.card-top{{display:flex;align-items:baseline;gap:8px;font-size:11px;color:var(--muted)}}
.tier{{font-weight:650;letter-spacing:.3px}}
.tags{{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.score{{margin-left:auto;font-variant-numeric:tabular-nums;color:var(--ink);font-weight:600;font-size:12px}}
.content{{margin:9px 0 11px;font-size:13.5px;line-height:1.55;display:-webkit-box;
 -webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}}
.meter{{height:3px;background:var(--line);border-radius:2px;overflow:hidden}}
.card-foot{{display:flex;justify-content:space-between;gap:8px;font-size:11px;color:var(--muted);margin-top:8px}}
.life.warn{{color:var(--warn)}}
.life.pin{{color:var(--ink);font-weight:600}}
.affect{{font-size:11px;color:var(--muted);margin-top:6px}}
.affect .dot{{display:inline-block;width:6px;height:6px;border-radius:50%;margin-right:5px}}
.badges{{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}}
.badge{{font-size:10.5px;border:1px solid var(--line);border-radius:999px;padding:1px 8px;color:var(--muted)}}
.badge.pin{{border-color:var(--ink);color:var(--ink)}}
.badge.warn{{border-color:var(--warn);color:var(--warn)}}
.row{{display:flex;align-items:center;gap:10px;margin:7px 0;font-size:12px}}
.row .name{{width:132px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--muted)}}
.row .track{{flex:1;height:5px;background:var(--line);border-radius:3px;overflow:hidden;display:block}}
.row .track>span{{display:block;height:100%;background:var(--accent);border-radius:3px}}
.row .pct{{width:36px;text-align:right;color:var(--muted);font-variant-numeric:tabular-nums}}
ol.chapters{{list-style:none;padding:0;margin:0}}
ol.chapters li{{display:flex;align-items:center;gap:10px;padding:9px 0;border-bottom:1px dashed var(--line);font-size:13px}}
ol.chapters li:last-child{{border-bottom:0}}
.arc{{font-size:10.5px;color:var(--accent);border:1px solid var(--accent);border-radius:5px;padding:0 6px}}
ol.chapters .n{{color:var(--muted);font-size:12px}}
ol.chapters .share{{margin-left:auto;color:var(--muted);font-size:12px;font-variant-numeric:tabular-nums}}
ul.grave{{list-style:none;padding:0;margin:0;font-size:12px;color:var(--muted)}}
ul.grave li{{display:flex;gap:12px;padding:7px 0;border-bottom:1px dashed var(--line)}}
ul.grave li:last-child{{border-bottom:0}}
ul.grave li b{{color:var(--ink);font-weight:500;min-width:120px;flex:none}}
.chips-row{{display:flex;flex-wrap:wrap;gap:8px}}
.chip{{border:1px solid var(--line);border-radius:999px;padding:3px 11px;font-size:12px}}
.muted{{color:var(--muted)}}.empty{{color:var(--muted)}}
.note{{margin-top:34px;padding-top:18px;border-top:1px solid var(--line);color:var(--muted);
 font-size:12px;line-height:1.8}}
.note b{{color:var(--ink);font-weight:600}}
</style></head><body><div class="wrap">

<div class="hero">
  <h1>{_esc(title)}</h1>
  <p>{_esc(summary)}</p>
  <div class="chips">
    <span class="chip-h">价值分布 <b>{HEALTH_ZH.get(snap["entropy_health"], snap["entropy_health"])}</b>
      　前三条占 {snap["attention"]["top3_share"]:.0%}</span>
    <span class="chip-h">结构 <b>{HEALTH_ZH.get(snap["structure_health"], snap["structure_health"])}</b>
      　孤岛 {snap["structure"]["isolated_share"]:.0%}</span>
    <span class="chip-h">自述 <b>rev {snap["self_model_revision"]}</b></span>
    <span class="chip-h">事件 <b>{snap["events"]}</b></span>
  </div>
</div>

<div class="kpis">
  <div class="kpi"><div class="v">{snap["nodes"]}</div><div class="l">在册记忆</div></div>
  <div class="kpi"><div class="v">{snap["tombstone"]}</div><div class="l">已主动放下</div></div>
  <div class="kpi"><div class="v">{len(dying)}</div><div class="l">30 天内将跌破遗忘线</div></div>
  <div class="kpi"><div class="v">{(sum(avg_life) / len(avg_life)):.0f}<span style="font-size:13px"> 天</span></div>
    <div class="l">平均剩余寿命（不含钉死）</div></div>
</div>

<section>
  <h2>还能记住多久<small>按剩余寿命分桶；跨过遗忘线就会被放手</small></h2>
  <div class="panel">{_lifespan_svg(palace, nodes, now)}</div>
</section>

<section>
  <h2>进出账<small>近 12 周：每周新增多少、遗忘多少</small></h2>
  <div class="panel">{_flow_svg(palace, nodes, now)}</div>
</section>

<section>
  <h2>记忆卡片<small>按重要度排序；条形是重要度，数字是四分量加权后的总分</small></h2>
  <div class="cards">{"".join(cards) or '<p class="muted">还没有记忆</p>'}</div>
</section>

<section>
  <div class="grid2">
    <div>
      <h2>自述<small>{_esc(narrative_meta)}</small></h2>
      <div class="panel">{f'<ol class="chapters">{chapters}</ol>' if chapters else '<p class="muted">还没有形成自述</p>'}</div>
    </div>
    <div>
      <h2>注意力份额<small>谁在占据「我在乎的东西」</small></h2>
      <div class="panel">{share_rows or '<p class="muted">无</p>'}</div>
    </div>
  </div>
</section>

<section>
  <h2>生效风格</h2>
  <div class="panel chips-row">{styles}</div>
</section>

<section>
  <h2>墓碑<small>被主动放下的东西，仍可溯源</small></h2>
  <div class="panel"><ul class="grave">{graveyard}</ul></div>
</section>

<p class="note">
这不是仪表盘，是一张<b>记忆的体检报告</b>。三条读法：<br>
① 曲线最先触底的那几条，就是系统认为最该放手的 —— 如果你觉得不对，把它们钉死。<br>
② 条形长但寿命短 = 现在很亮、但很快会淡；这不一定是坏事，多数琐事本该如此。<br>
③ 墓碑不是失败，是取舍。全部保留，等于没有取舍。
</p>
</div></body></html>"""
