"""cli.py - 把记忆系统做成 agent 能直接调用的命令行工具。

为什么要有 CLI：一个库只有被 agent 在**每一步**能便宜地调用，才算真的接进去了。
agent 需要的是三件事，而不是一堆类：

  1. 写一笔：`remember`  —— 一句话、可带标签/钉死，名字自动生成并回传
  2. 取一块：`context`   —— 受预算约束、可直接塞进 system prompt 的文本块（核心）
  3. 睡一觉：`cycle`     —— 沉淀、遗忘、自述、反思、自修正，一次跑完

其余命令（recall / status / explain / outcome / pin / forget / style）都是围绕这三件事的可解释补丁：
为什么命中、为什么忘了、以及把用户的纠正变成情绪与风格证据。

存储：默认 `~/.memory-palace/store.json`，可用 `--store` 或环境变量 `MEMORY_PALACE_STORE` 覆盖。
所有写操作都是原子的（tempfile + fsync + replace），读操作不落盘。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from typing import Any

from ._console import ensure_utf8_console
from .palace import MemoryPalace
from .render import html_report, status_line, text_report

DEFAULT_STORE = os.path.join(os.path.expanduser("~"), ".memory-palace", "store.json")
OUTCOME_ALIASES = {
    "loss": "loss", "亏": "loss", "止损": "loss",
    "win": "win", "赚": "win", "成": "win",
    "error": "error", "错": "error",
    "contradicted": "contradicted", "被反驳": "contradicted", "纠错": "contradicted",
    "confirmed": "confirmed", "确认": "confirmed",
    "missed": "missed", "没想起": "missed",
    "recalled": "recalled", "想起": "recalled",
}


# ---------------------------------------------------------------- 基础设施


def resolve_store(explicit: str | None) -> str:
    path = explicit or os.environ.get("MEMORY_PALACE_STORE") or DEFAULT_STORE
    return os.path.abspath(os.path.expanduser(path))


def open_palace(path: str, autosave: bool = False) -> MemoryPalace:
    return MemoryPalace(path=path, autosave=autosave)


def parse_tags(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    return tuple(t.strip() for t in raw.split(",") if t.strip())


def auto_name(content: str) -> str:
    """内容哈希做名字：同一条内容重复写入不会长出两条记忆。"""
    digest = hashlib.blake2b(content.encode("utf-8"), digest_size=4).hexdigest()
    return f"m:{digest}"


def _fmt_affect(node) -> str:
    affect = node.affect
    if affect.source == "neutral":
        return "情绪 中性"
    return f"情绪 v={affect.valence:+.2f}/a={affect.arousal:.2f}（{affect.source}）"


# ---------------------------------------------------------------- 上下文块


def build_context(
    palace: MemoryPalace,
    query: str | None = None,
    limit: int = 6,
    budget: int = 900,
    names: list[str] | None = None,
) -> dict[str, Any]:
    """把「现在该想起什么」变成一段受预算约束的文本。

    排序不是纯相似度，而是三档（这是这个函数唯一需要解释的决定）：

      1. **钉死的记忆永远在**。硬规则的价值恰恰在于「不相关时也必须遵守」——
         如果只在语义命中时才注入，那它就不是规则，只是资料。
      2. **背景两条**：当前最重要的记忆，避免 agent 只看眼前问题。
      3. **其余按混合分**：0.6×语义（归一化）+ 0.4×重要性（归一化）。
         只用相似度会让「像但没价值」的东西挤掉「不像但重要」的东西。

    预算按字符算；钉死条目超预算时仍然保留，并在提示里说明超了多少。
    """
    now = palace.now()
    cfg = palace._config
    nodes = palace.graph.all_nodes()
    if not nodes:
        return {
            "how": "空库",
            "budget": budget,
            "used": 0,
            "total_candidates": 0,
            "items": [],
            "narrative": None,
            "warnings": ["这套记忆还是空的：先用 remember 写入，或跑一次 cycle"],
        }

    assessment = palace.value.assess(palace.graph, now, palace.logic.support_map())
    max_importance = max((v["total"] for v in assessment.values()), default=0.0) or 1e-9
    rows = (
        {r["name"]: r for r in palace.score_breakdown(query)}
        if query
        else {}
    )
    max_score = max((r["score"] for r in rows.values()), default=0.0) or 1e-9

    reasons: dict[str, str] = {}
    if names:
        pool = [n for n in names if n in assessment]
        reasons = {n: "指定" for n in pool}
        how = "指定名单"
    else:
        for node in nodes:
            if node.pinned:
                reasons[node.name] = "钉死"
        if query:
            # 有具体问题时，额外带两条「当前最重要」当背景，避免只见树木
            background = sorted(assessment, key=lambda n: (-assessment[n]["total"], n))[:2]
            for name in background:
                reasons.setdefault(name, "常驻")
            for name in rows:
                if rows[name]["score"] > 0:
                    reasons.setdefault(name, "相关")
            how = f"语义检索「{query}」"
        else:
            # 没有问题时，就按重要性取满预算内的前 limit 条
            ranked = sorted(assessment, key=lambda n: (-assessment[n]["total"], n))[:limit]
            for name in ranked:
                reasons.setdefault(name, "常驻")
            how = "当前显著度"

    def blend(name: str) -> float:
        sem = rows.get(name, {}).get("score", 0.0) / max_score
        imp = assessment[name]["total"] / max_importance
        return 0.6 * sem + 0.4 * imp

    if not names:
        pool = list(reasons)
    pool.sort(key=lambda n: (0 if reasons.get(n) == "钉死" else 1, -blend(n), n))

    items: list[dict[str, Any]] = []
    used = 0
    for name in pool:
        node = palace.graph.get(name)
        if node is None:
            continue
        parts = assessment[name]
        fidelity = node.affect.fidelity(cfg.affect_distortion)
        label = node.tags[0] if node.tags else ("规则" if node.tier == "core" else "记忆")
        line = f"- [{label}] {node.content}"
        markers = []
        if node.pinned:
            markers.append("钉死")
        if fidelity < cfg.narrative_reliability_warn:
            markers.append(f"细节可能失真 {fidelity:.2f}")
        if node.consolidated_from:
            markers.append(f"由 {len(node.consolidated_from)} 条沉淀")
        if markers:
            line += "（" + "；".join(markers) + "）"
        pinned_line = reasons.get(name) == "钉死"
        if not pinned_line and used + len(line) > budget and items:
            break
        if not pinned_line and len(items) >= limit:
            break
        items.append(
            {
                "name": name,
                "text": line,
                "why": reasons.get(name, "相关"),
                "importance": parts["total"],
                "pinned": node.pinned,
                "fidelity": round(fidelity, 4),
            }
        )
        used += len(line)

    head = palace.narrative_head()
    warnings: list[str] = []
    if used > budget:
        warnings.append(f"钉死规则较长，已超出预算 {used - budget} 字（规则优先于预算）")
    unreliable = [i for i in items if i["fidelity"] < cfg.narrative_reliability_warn]
    if unreliable:
        warnings.append(
            f"{len(unreliable)} 条记忆情绪强、可靠度 < {cfg.narrative_reliability_warn:.2f}："
            "引用其细节时标注不确定"
        )
    dropped = len(assessment) - len(items)
    if dropped > 0:
        warnings.append(f"另有 {dropped} 条未入选（预算/排序），需要时用 recall 深挖")

    styles = palace.active_style()
    if styles:
        warnings.append("生效输出风格：" + "；".join(styles))
    pending = palace.snapshot()["pending_style_conflicts"]
    if pending:
        warnings.append(
            f"{len(pending)} 条风格因对立证据不足被停用："
            + "、".join(p["key"] for p in pending)
        )
    dist = palace.entropy()
    if dist["health"] == "flat":
        warnings.append("重要性分布几乎无梯度：这套记忆还没有取舍，考虑钉死真正要紧的")
    elif dist["health"] == "narrow":
        warnings.append("重心压在一件事上（单项占比过高），记忆可能在收窄")
    if len(nodes) >= 5 and dist["structure_health"] in ("isolated", "fragmented"):
        warnings.append("记忆之间几乎没有连接：现在只能逐条查，推不出新结论")

    return {
        "how": how,
        "budget": budget,
        "used": used,
        "total_candidates": len(assessment),
        "items": items,
        "narrative": head,
        "warnings": warnings,
    }


def format_context(ctx: dict[str, Any]) -> str:
    lines = [
        f"# 记忆上下文（候选 {ctx['total_candidates']} 条 → 取 {len(ctx['items'])} 条，"
        f"{ctx['used']}/{ctx['budget']} 字，{ctx['how']}）"
    ]
    if not ctx["items"]:
        lines.append("（空：这套记忆里还没有相关条目）")
    else:
        lines.append("")
        lines.extend(item["text"] for item in ctx["items"])

    head = ctx["narrative"]
    if head:
        arcs = " → ".join(
            f"{c['arc']}「{c['title']}」{len(c['nodes'])}条" for c in head.get("chapters", [])
        )
        lines.append("")
        lines.append(
            f"## 自述（第 {head['rev']} 版）\n"
            f"{arcs or '无章节'}｜可靠度 {head['reliability']:.2f}｜单主题占比 {head['monopoly']:.2f}"
        )
    if ctx["warnings"]:
        lines.append("")
        lines.append("## 提示")
        lines.extend(f"- {w}" for w in ctx["warnings"])
    return "\n".join(lines)


# ---------------------------------------------------------------- 子命令


def cmd_init(args) -> int:
    path = resolve_store(args.store)
    if os.path.exists(path) and not args.force:
        print(f"已存在：{path}（要重建请加 --force）")
        return 0
    palace = open_palace(path)
    palace.save()
    print(f"已初始化：{path}")
    return 0


def cmd_remember(args) -> int:
    content = " ".join(args.content).strip()
    if not content:
        print("内容为空", file=sys.stderr)
        return 2
    path = resolve_store(args.store)
    palace = open_palace(path)
    name = args.name or auto_name(content)
    if name in palace.graph and not args.replace:
        print(f"已存在同名记忆 {name}，未覆盖（要覆盖加 --replace）")
        return 0
    node = palace.remember(
        name,
        content,
        tags=parse_tags(args.tags),
        tier=args.tier,
        pinned=args.pin,
        valence=args.valence,
        arousal=args.arousal,
        kind=args.kind or "",
    )
    palace.save()
    print(f"已记住 {node.name}｜{_fmt_affect(node)}｜层级 {node.tier}")
    return 0


def cmd_recall(args) -> int:
    path = resolve_store(args.store)
    palace = open_palace(path)
    names = list(args.name) if args.name else None
    hits = palace.recall(names=names, query=args.query, limit=args.k)
    if args.json:
        print(json.dumps(
            [{"name": n.name, "content": n.content, "tags": list(n.tags),
              "importance": palace.importance_of(n.name)} for n in hits],
            ensure_ascii=False, indent=2))
        return 0
    if not hits:
        print("（没有命中）")
        return 0
    for node in hits:
        imp = palace.importance_of(node.name) or {}
        print(f"- {node.name}｜{node.content}｜重要度 {imp.get('total', 0):.3f}")
    return 0


def cmd_context(args) -> int:
    path = resolve_store(args.store)
    palace = open_palace(path)
    ctx = build_context(
        palace,
        query=args.query,
        limit=args.k,
        budget=args.budget,
        names=list(args.name) if args.name else None,
    )
    if args.status_only:
        rev = (ctx.get("narrative") or {}).get("rev")
        print(status_line(ctx, rev))
        return 0
    if args.json:
        print(json.dumps(ctx, ensure_ascii=False, indent=2))
    else:
        print(format_context(ctx))
    return 0


def cmd_view(args) -> int:
    """终端视图：不离开命令行就能看到「谁快被忘掉了」。"""
    palace = open_palace(resolve_store(args.store))
    print(text_report(palace, limit=args.limit, horizon_days=args.days))
    return 0


def cmd_render(args) -> int:
    """生成自包含 HTML 面板（含衰减曲线），双击即可看。"""
    palace = open_palace(resolve_store(args.store))
    out = os.path.abspath(os.path.expanduser(args.out))
    parent = os.path.dirname(out)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(html_report(palace, title=args.title, horizon_days=args.days))
    print(f"已生成 {out}（{os.path.getsize(out) / 1024:.1f} KB，自包含，无需联网）")
    return 0


def cmd_cycle(args) -> int:
    path = resolve_store(args.store)
    palace = open_palace(path)
    report = palace.run_cycle()
    palace.save()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    narrative = report["narrative"]
    print(
        f"节点 {report['nodes']}｜沉淀 {len(report['consolidated'])} 条｜"
        f"抓住 {len(report['grasped'])} 条｜降级 {len(report['demoted'])} 条｜"
        f"遗忘 {len(report['forgotten'])} 条"
    )
    print(f"自述：{narrative['text']}")
    correction = report.get("selfcorrect") or {}
    if correction.get("applied"):
        print(f"自修正：{len(correction['applied'])} 项已生效（观察窗后验证）")
    return 0


def cmd_status(args) -> int:
    path = resolve_store(args.store)
    palace = open_palace(path)
    snap = palace.snapshot()
    if args.json:
        print(json.dumps(snap, ensure_ascii=False, indent=2))
        return 0
    size = os.path.getsize(path) if os.path.exists(path) else 0
    print(f"存储 {path}（{size / 1024:.1f} KB）")
    print(f"节点 {snap['nodes']}｜边 {snap['edges']}｜事件 {snap['events']}｜墓碑 {snap['tombstone']}")
    print(f"层级 {snap['tiers']}")
    print(f"价值健康 {snap['entropy_health']}｜结构 {snap['structure_health']}"
          f"（孤立 {snap['structure']['isolated_share']:.0%}）")
    print(f"生效风格 {snap['active_style'] or '（无）'}")
    correction = snap["selfcorrect"]
    if correction["pending"] or correction["blocked"]:
        print(f"自修正：待验证 {len(correction['pending'])}｜永久收权 {correction['blocked']}")
    return 0


def cmd_explain(args) -> int:
    path = resolve_store(args.store)
    palace = open_palace(path)
    detail = palace.semantic_explain(args.query)
    print("概念投影：", json.dumps(detail.get("concepts", {}), ensure_ascii=False))
    if detail.get("ambiguous"):
        print("多义词裁决：", json.dumps(detail["ambiguous"], ensure_ascii=False))
    if detail.get("learned"):
        print("从语料学到的词：", json.dumps(detail["learned"], ensure_ascii=False))
    rows = palace.score_breakdown(args.query)[: args.k]
    print("逐条打分：")
    for row in rows:
        print(
            f"- {row['name']}｜总分 {row['score']:.3f} = 语义 {row['semantic']:.3f}"
            f" + 标签 {row['tag']:.3f} + 时近 {row['recency']:.3f}"
        )
    return 0


def cmd_outcome(args) -> int:
    outcome = OUTCOME_ALIASES.get(args.result)
    if outcome is None:
        print(f"未知结果类型 {args.result}（可选 {sorted(set(OUTCOME_ALIASES.values()))}）", file=sys.stderr)
        return 2
    path = resolve_store(args.store)
    palace = open_palace(path)
    node = palace.feel_outcome(args.name, outcome)
    if node is None:
        print(f"没有这条记忆：{args.name}", file=sys.stderr)
        return 1
    palace.save()
    print(f"{node.name}｜{_fmt_affect(node)}｜依据 {node.affect.drivers}")
    return 0


def cmd_pin(args) -> int:
    path = resolve_store(args.store)
    palace = open_palace(path)
    node = palace.pin(args.name, pinned=not args.unpin)
    if node is None:
        print(f"没有这条记忆：{args.name}", file=sys.stderr)
        return 1
    palace.save()
    print(f"{node.name}｜{'已钉死' if node.pinned else '已解钉'}")
    return 0


def cmd_forget(args) -> int:
    path = resolve_store(args.store)
    palace = open_palace(path)
    if not palace.drop(args.name, reason=args.reason):
        print(f"没有这条记忆：{args.name}", file=sys.stderr)
        return 1
    palace.save()
    print(f"已丢弃 {args.name}（进墓碑，可用代码 restore 复活）")
    return 0


def cmd_style(args) -> int:
    path = resolve_store(args.store)
    palace = open_palace(path)
    if args.action == "observe":
        if args.sign not in (1, -1):
            print("--sign 只能是 +1 或 -1", file=sys.stderr)
            return 2
        opposes = parse_tags(args.opposes)
        palace.observe_style(args.key, args.statement, args.sign, opposes=opposes)
        palace.save()
        print(f"已记录风格证据 {args.key} {args.sign:+d}：{args.statement}")
        return 0
    active = palace.active_style()
    print("生效风格：" + ("；".join(active) if active else "（无，证据还不够或存在对立）"))
    pending = palace.snapshot()["pending_style_conflicts"]
    if pending:
        print("悬而未决：" + "、".join(f"{p['key']}({p['reason']})" for p in pending))
    return 0


# ---------------------------------------------------------------- 入口


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--store", help=f"记忆库路径（默认 {DEFAULT_STORE}）")

    parser = argparse.ArgumentParser(
        prog="memory-palace",
        description="会遗忘、会被情绪加固、会讲自己故事的记忆系统（CLI）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", parents=[common], help="初始化记忆库")
    p.add_argument("--force", action="store_true", help="已存在时重建（会清空旧内容）")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("remember", parents=[common], help="写入一条记忆")
    p.add_argument("content", nargs="+", help="记忆内容")
    p.add_argument("--name", help="名字（默认按内容哈希生成，可幂等重复写）")
    p.add_argument("--tags", help="逗号分隔的标签")
    p.add_argument("--tier", default="event",
                   choices=["trace", "ephemeral", "event", "fact", "core"])
    p.add_argument("--pin", action="store_true", help="钉死：不衰减、不参与自动遗忘")
    p.add_argument("--valence", type=float, default=None, help="效价 [-1,1]（不传则由文本推断）")
    p.add_argument("--arousal", type=float, default=None, help="唤醒 [0,1]（不传则由文本推断）")
    p.add_argument("--kind", default="",
                   choices=["", "working", "episodic", "semantic", "procedural", "identity"])
    p.add_argument("--replace", action="store_true", help="同名时覆盖")
    p.set_defaults(func=cmd_remember)

    p = sub.add_parser("recall", parents=[common], help="检索（语义 / 名字）")
    p.add_argument("query", nargs="?", help="语义查询")
    p.add_argument("--name", action="append", help="按名字精确取（可多次）")
    p.add_argument("-k", type=int, default=6, help="返回条数（默认 6）")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_recall)

    p = sub.add_parser("context", parents=[common], help="产出可注入 prompt 的记忆块")
    p.add_argument("--query", help="按当前任务检索；不传则按显著度")
    p.add_argument("--name", action="append", help="指定必须包含的记忆（可多次）")
    p.add_argument("-k", type=int, default=6, help="最多条数（默认 6）")
    p.add_argument("--budget", type=int, default=900, help="字符预算（默认 900）")
    p.add_argument("--json", action="store_true")
    p.add_argument("--status-only", action="store_true",
                   help="只输出一行状态（记忆 5/12 · 钉死1 · 存疑2 · 412/800字），用于贴给用户看")
    p.set_defaults(func=cmd_context)

    p = sub.add_parser("view", parents=[common], help="终端视图：谁重要、谁快被忘掉")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--days", type=float, default=30, help="寿命预警窗口（天）")
    p.set_defaults(func=cmd_view)

    p = sub.add_parser("render", parents=[common], help="生成自包含 HTML 记忆面板（含衰减曲线）")
    p.add_argument("--out", default="memory-palace.html", help="输出路径")
    p.add_argument("--title", default="记忆宫殿")
    p.add_argument("--days", type=float, default=60, help="衰减曲线跨度（天）")
    p.set_defaults(func=cmd_render)

    p = sub.add_parser("cycle", parents=[common], help="跑一轮代谢（沉淀/遗忘/自述/反思/自修正）")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_cycle)

    p = sub.add_parser("status", parents=[common], help="看库的体检报告")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("explain", parents=[common], help="这条查询凭什么命中 / 归到哪个概念")
    p.add_argument("query")
    p.add_argument("-k", type=int, default=3)
    p.set_defaults(func=cmd_explain)

    p = sub.add_parser("outcome", parents=[common], help="把结果反馈成情绪（不改记忆强度）")
    p.add_argument("name")
    p.add_argument("result", help="loss/win/error/contradicted/confirmed/missed/recalled（支持中文）")
    p.set_defaults(func=cmd_outcome)

    p = sub.add_parser("pin", parents=[common], help="钉死一条记忆")
    p.add_argument("name")
    p.add_argument("--unpin", action="store_true", help="解钉")
    p.set_defaults(func=cmd_pin)

    p = sub.add_parser("forget", parents=[common], help="显式丢弃一条记忆（进墓碑）")
    p.add_argument("name")
    p.add_argument("--reason", default="user_requested")
    p.set_defaults(func=cmd_forget)

    p = sub.add_parser("style", parents=[common], help="风格证据与生效状态")
    p.add_argument("action", choices=["observe", "active"])
    p.add_argument("key", nargs="?", help="风格键（observe 时必填）")
    p.add_argument("statement", nargs="?", help="风格描述（observe 时必填）")
    p.add_argument("--sign", type=int, default=1, help="+1 支持 / -1 反证")
    p.add_argument("--opposes", help="与哪些风格键对立（逗号分隔）")
    p.set_defaults(func=cmd_style)

    return parser


def main(argv: list[str] | None = None) -> int:
    ensure_utf8_console()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "style" and args.action == "observe" and not (args.key and args.statement):
        parser.error("style observe 需要 key 与 statement")
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
