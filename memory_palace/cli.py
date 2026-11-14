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
import re
import sys
from datetime import datetime
from typing import Any

from ._console import ensure_utf8_console
from .palace import MemoryPalace
from .scope import GLOBAL, SCOPE_MODES, label as scope_label, scopes_for
from .tree import build_tree, render_tree, tree_stats

DEFAULT_STORE = os.path.join(os.path.expanduser("~"), ".memory-palace", "store.sqlite")
# 0.9.x 的默认存储。第一次跑 0.10+ 时会被自动导入到 SQLite，原文件保留为 .migrated。
LEGACY_STORE = os.path.join(os.path.expanduser("~"), ".memory-palace", "store.json")
# `--backend` 是全局开关，所有子命令都用同一个值；用模块级变量避免每个调用点都传一遍。
_DEFAULT_BACKEND = "auto"
SCOPE_VIEW_ZH = {
    "auto": "全局+本项目",
    "global": "仅全局",
    "project": "仅本项目",
    "session": "仅本次会话",
    "all": "全部作用域",
}
_RELATIVE = re.compile(r"^[+-]?(\d+(?:\.\d+)?)d$")


def parse_moment(text: str | None, now: float) -> float | None:
    """把「多久以前 / 哪一天」翻成 epoch 秒。

    支持两种写法，因为两种直觉都合理：
      · 相对：`30d` = 30 天前（给 agent 用，它不擅长算日期）
      · 绝对：`2026-10-01`（给人用，他要的是「那天的我怎么想」）
    """
    if not text:
        return None
    raw = text.strip()
    match = _RELATIVE.match(raw)
    if match:
        return now - float(match.group(1)) * 86400.0
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(raw, fmt).timestamp()
        except ValueError:
            continue
    raise SystemExit(f"看不懂的时间: {text!r}（用 30d 或 2026-10-01）")
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
    path = explicit or os.environ.get("MEMORY_PALACE_STORE")
    if path:
        return os.path.abspath(os.path.expanduser(path))
    # 没有任何显式指定时的默认位置。0.9.x 的老库在这里，自动搬一次。
    if os.path.exists(DEFAULT_STORE) or not os.path.exists(LEGACY_STORE):
        return DEFAULT_STORE
    return _migrate_legacy(LEGACY_STORE, DEFAULT_STORE)


def _migrate_legacy(legacy: str, target: str) -> str:
    """把 0.9.x 的 JSON 库导入 SQLite，原文件改名保留（不删）。

    自动做而不是问，是因为「升级后记忆还在」不该是个需要用户回答的问题；
    但必须**保留原件**，因为迁移一旦有 bug，那份 JSON 是唯一的退路。
    """
    from .store import open_store

    try:
        src = open_store(legacy, backend="json")
        dst = open_store(target, backend="sqlite")
        loaded = src.load()
        saved = dst.save(loaded.payload, reason="migrate")
        os.replace(legacy, legacy + ".migrated")
        print(
            f"已把旧库迁移到 SQLite：{target}（revision {saved.revision}）\n"
            f"原文件保留为 {legacy}.migrated",
            file=sys.stderr,
        )
        return target
    except Exception as exc:  # noqa: BLE001 - 迁移失败就继续用老库，绝不吞掉用户数据
        print(f"旧库迁移失败（继续使用 {legacy}）：{exc}", file=sys.stderr)
        return legacy


def open_palace(
    path: str,
    project: str | None = None,
    autosave: bool = False,
    backend: str | None = None,
) -> MemoryPalace:
    """打开记忆库，并带上「当前是哪个项目」——它决定默认写哪里、默认读哪些。"""
    return MemoryPalace(
        path=path,
        autosave=autosave,
        project=project,
        backend=backend or _DEFAULT_BACKEND,
    )


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
    scope_mode: str = "auto",
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
    # 长驻进程（MCP/HTTP）里，别的进程可能刚写过：先对齐再取上下文
    palace.refresh()
    now = palace.now()
    cfg = palace._config
    # 视窗：auto = 全局 + 当前项目。这是「跨项目但不串味」的那条线。
    scopes = scopes_for(scope_mode, palace.project)
    nodes = [
        n for n in palace.graph.all_nodes()
        if scopes is None or n.scope in scopes
    ]
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
    if scopes is not None:
        assessment = {
            k: v for k, v in assessment.items()
            if (node := palace.graph.get(k)) is not None and node.scope in scopes
        }
    max_importance = max((v["total"] for v in assessment.values()), default=0.0) or 1e-9
    rows = (
        {r["name"]: r for r in palace.score_breakdown(query, scopes=scopes)}
        if query
        else {}
    )
    max_score = max((r["score"] for r in rows.values()), default=0.0) or 1e-9

    # 「相关」必须由语义或标签判，不能用总分 —— 总分里含时近分量，人人有份。
    # 这一步是弃答的前提：如果连一条真正沾边的都没有，就不该硬凑背景。
    # 两层判据，缺一不可：
    #   绝对——语义分要过弃答线（底噪之上）；
    #   相对——还要明显领先于同批候选（乘 semantic_relative_floor），
    #          否则「最高 0.83、其余 0.01」时，那 0.01 也会被当成命中。
    best_sem = max((row["semantic"] for row in rows.values()), default=0.0)
    rel_floor = best_sem * cfg.semantic_relative_floor
    relevant = {
        name
        for name, row in rows.items()
        if (row["semantic"] >= max(cfg.semantic_min_score, rel_floor) and row["semantic"] >= 1e-9)
        or row["tag"] > 0
    }
    weak = best_sem < cfg.abstain_semantic_floor
    abstain = bool(query) and not names and (weak or not relevant)

    reasons: dict[str, str] = {}
    if names:
        pool = [n for n in names if n in assessment]
        reasons = {n: "指定" for n in pool}
        how = "指定名单"
    elif abstain:
        # 弃答：只留钉死的硬规则。规则与问题无关也要遵守，这不叫「猜」；
        # 但把两条最重的记忆端上去冒充答案，才叫猜。
        for node in nodes:
            if node.pinned and node.name in assessment:
                reasons[node.name] = "钉死"
        how = f"语义检索「{query}」· 无匹配"
    else:
        for node in nodes:
            if node.pinned and node.name in assessment:
                reasons[node.name] = "钉死"
        if query:
            # 先标真正相关的，再补「当前最重要」当背景（顺序反了会把命中标成背景）
            for name in relevant:
                reasons.setdefault(name, "相关")
            background = sorted(assessment, key=lambda n: (-assessment[n]["total"], n))[:2]
            for name in background:
                reasons.setdefault(name, "常驻")
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

    # budget <= 0 的语义就是「什么都不要」：调用方明确不要时，不该用「至少给一条」
    # 的兜底覆盖它。原来 budget=0 也会返回一条，等于让预算参数说了不算。
    if budget <= 0:
        pool = []
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
        # 语用标记：模型必须知道「这是禁令」还是「这是暗示」——
        # 把暗示当规则执行，比不执行更糟。
        act = getattr(node.speech, "act", "statement")
        if act in ("complaint", "hint") or getattr(node.speech, "hedged", False):
            markers.append(f"{node.speech.label()}·未确认")
        elif act in ("prohibition", "directive"):
            markers.append(node.speech.label())
        elif act == "withdrawal":
            markers.append("撤回（不等于同意）")
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
        if abstain:
            warnings.append(f"另有 {dropped} 条与本问题不相关，已排除")
        else:
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
    dist = palace.entropy(scopes)
    if dist["health"] == "flat":
        warnings.append("重要性分布几乎无梯度：这套记忆还没有取舍，考虑钉死真正要紧的")
    elif dist["health"] == "narrow":
        warnings.append("重心压在一件事上（单项占比过高），记忆可能在收窄")
    if len(nodes) >= 5 and dist["structure_health"] in ("isolated", "fragmented"):
        warnings.append("记忆之间几乎没有连接：现在只能逐条查，推不出新结论")

    if abstain:
        warnings.insert(0, f"没有找到与「{query}」相关的记忆：不要凭印象补全，宁可说不知道或反问")

    return {
        "how": how,
        "scope": scope_mode,
        "budget": budget,
        "used": used,
        "total_candidates": len(assessment),
        "items": items,
        "narrative": head,
        "warnings": warnings,
        "abstain": abstain,
    }


def format_context(ctx: dict[str, Any]) -> str:
    lines = [
        f"# 记忆上下文（候选 {ctx['total_candidates']} 条 → 取 {len(ctx['items'])} 条，"
        f"{ctx['used']}/{ctx['budget']} 字，{ctx['how']}"
        + (f"，{SCOPE_VIEW_ZH.get(ctx.get('scope', 'auto'), '')}" if ctx.get("scope") else "")
        + "）"
    ]
    if not ctx["items"]:
        lines.append("（空：这套记忆里还没有相关条目）")
    else:
        lines.append("")
        lines.extend(item["text"] for item in ctx["items"])
    if ctx.get("abstain"):
        lines.append("")
        lines.append("（没有相关记忆。不要凭印象补全；不确定就说不确定，或反问。）")

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
    palace = open_palace(path, getattr(args, "project", None))
    palace.save()
    print(f"已初始化：{path}")
    return 0


def cmd_remember(args) -> int:
    content = " ".join(args.content).strip()
    if not content:
        print("内容为空", file=sys.stderr)
        return 2
    path = resolve_store(args.store)
    palace = open_palace(path, getattr(args, "project", None))
    name = args.name or auto_name(content)
    if name in palace.graph:
        if not args.replace:
            print(f"已存在同名记忆 {name}，未覆盖（要覆盖加 --replace）")
            return 0
        # 覆盖 = 旧的进墓碑、新的同名写入。以前这里只是把 supersedes 里塞了同名，
        # 而 supersedes 会跳过「取代自己」，于是 add_node 发现同名就原地返回 ——
        # --replace 实际什么都没做，却打印了「已记住」。
        palace.drop(name, reason="replaced")
    supersedes = list(args.supersedes or [])
    node = palace.remember(
        name,
        content,
        tags=parse_tags(args.tags),
        tier=args.tier,
        pinned=args.pin,
        valence=args.valence,
        arousal=args.arousal,
        kind=args.kind or "",
        supersedes=supersedes,
        merge=not args.no_merge,
        scope=args.scope or "",
        auto_tier=getattr(args, "auto_tier", False),
    )
    palace.save()
    if node.name != name:
        # 去重命中：没有新建节点，而是强化了已有的那条
        print(f"已合并到 {node.name}（内容几乎相同，未新建）｜累计想起 {node.access_count} 次")
        return 0
    act = getattr(node.speech, "act", "statement")
    print(f"已记住 {node.name}｜{_fmt_affect(node)}｜层级 {node.tier}｜{node.speech.label()}"
          + ("（未确认）" if node.speech.hedged or act in ("complaint", "hint") else ""))
    if node.supersedes:
        print(f"　取代了 {', '.join(node.supersedes)}（它们仍可 as-of 查询，{int(palace._config.history_retention_days)} 天后回收）")
    return 0


def cmd_recall(args) -> int:
    path = resolve_store(args.store)
    palace = open_palace(path, getattr(args, "project", None))
    names = list(args.name) if args.name else None
    as_of = parse_moment(getattr(args, "as_of", None), palace.now())
    hits = palace.recall(
        names=names, query=args.query, limit=args.k, as_of=as_of,
        scopes=scopes_for(args.scope, palace.project),
    )
    if args.json:
        print(json.dumps(
            [{"name": n.name, "content": n.content, "tags": list(n.tags),
              "importance": palace.importance_of(n.name)} for n in hits],
            ensure_ascii=False, indent=2))
        return 0
    if not hits:
        print("（没有命中）")
        return 0
    if as_of is not None:
        print(f"（回看 {datetime.fromtimestamp(as_of):%Y-%m-%d %H:%M}：当时为真的记忆）")
    for node in hits:
        imp = palace.importance_of(node.name) or {}
        mark = ""
        if node.is_retired():
            mark = f"｜已于 {datetime.fromtimestamp(node.valid_to):%Y-%m-%d} 失效（被 {node.superseded_by} 取代）"
        print(f"- {node.name}｜{node.content}｜重要度 {imp.get('total', 0):.3f}{mark}")
    return 0


def cmd_context(args) -> int:
    path = resolve_store(args.store)
    palace = open_palace(path, getattr(args, "project", None))
    ctx = build_context(
        palace,
        query=args.query,
        limit=args.k,
        budget=args.budget,
        names=list(args.name) if args.name else None,
        scope_mode=args.scope,
    )
    if args.tree:
        scores = {
            r["name"]: r["score"]
            for r in palace.score_breakdown(args.query)
        } if args.query else {}
        tree = build_tree(palace, scores=scores)
        rendered = render_tree(
            tree,
            expand=args.expand,
            budget=args.budget,
            leaf_limit=max(1, args.k // 2 or 1),
        )
        head = ctx.get("narrative")
        if head and rendered["used"] + 80 <= args.budget:
            # 自述压成一行：预算够就带上，不够就让位给记忆本身
            tail = (
                f"— 自述 rev{head['rev']} · 单主题 {head['monopoly']:.2f}"
                f" · 可靠度 {head['reliability']:.2f}"
            )
            rendered["text"] = rendered["text"] + "\n" + tail
            rendered["used"] += len(tail)
        if args.compare:
            flat = format_context(ctx)
            rendered["flat_used"] = len(flat)
            rendered["saving"] = round(1 - len(rendered["text"]) / max(1, len(flat)), 4)
        if args.json:
            print(json.dumps({**rendered, "shape": tree_stats(tree)}, ensure_ascii=False, indent=2))
        else:
            print(rendered["text"])
            if args.compare:
                print(
                    f"\n[对比] 扁平 {rendered['flat_used']} 字 → 树 {rendered['used']} 字，"
                    f"省 {rendered['saving']:.0%}"
                )
        return 0
    if args.status_only:
        rev = (ctx.get("narrative") or {}).get("rev")
        from .render import status_line

        print(status_line(ctx, rev, abstain=bool(ctx.get("abstain"))))
        return 0
    if args.json:
        print(json.dumps(ctx, ensure_ascii=False, indent=2))
    else:
        print(format_context(ctx))
    return 0


def cmd_view(args) -> int:
    """终端视图：不离开命令行就能看到「谁快被忘掉了」。"""
    palace = open_palace(resolve_store(args.store), getattr(args, "project", None))
    from .render import text_report

    print(text_report(
        palace, limit=args.limit, horizon_days=args.days,
        scope=getattr(args, "scope", "auto"),
    ))
    return 0


def cmd_render(args) -> int:
    """生成自包含 HTML 面板（含衰减曲线），双击即可看。"""
    palace = open_palace(resolve_store(args.store), getattr(args, "project", None))
    out = os.path.abspath(os.path.expanduser(args.out))
    parent = os.path.dirname(out)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        from .render import html_report

        f.write(html_report(
            palace, title=args.title, horizon_days=args.days,
            scope=getattr(args, "scope", "auto"),
        ))
    print(f"已生成 {out}（{os.path.getsize(out) / 1024:.1f} KB，自包含，无需联网）")
    return 0


def cmd_cycle(args) -> int:
    path = resolve_store(args.store)
    palace = open_palace(path, getattr(args, "project", None))
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
    palace = open_palace(path, getattr(args, "project", None))
    snap = palace.snapshot()
    if args.json:
        print(json.dumps(snap, ensure_ascii=False, indent=2))
        return 0
    size = os.path.getsize(path) if os.path.exists(path) else 0
    print(f"存储 {path}（{size / 1024:.1f} KB）")
    retired = snap.get("retired", 0)
    print(f"节点 {snap['nodes']}｜边 {snap['edges']}｜事件 {snap['events']}｜墓碑 {snap['tombstone']}"
          + (f"｜已失效 {retired}（可 as-of 回看）" if retired else ""))
    print(f"层级 {snap['tiers']}")
    counts = palace.scope_counts()
    if counts:
        shown = "｜".join(f"{scope_label(k)} {v}" for k, v in counts.items())
        print(f"作用域 {shown}（当前 {scope_label(palace.project)}）")
    print(f"价值健康 {snap['entropy_health']}｜结构 {snap['structure_health']}"
          f"（孤立 {snap['structure']['isolated_share']:.0%}）")
    print(f"生效风格 {snap['active_style'] or '（无）'}")
    correction = snap["selfcorrect"]
    if correction["pending"] or correction["blocked"]:
        print(f"自修正：待验证 {len(correction['pending'])}｜永久收权 {correction['blocked']}")
    return 0


def cmd_promote(args) -> int:
    """把一条记忆升为跨项目。显式动作，不是自动泛化。"""
    palace = open_palace(resolve_store(args.store), getattr(args, "project", None))
    node = palace.promote(args.name, args.to)
    if node is None:
        print(f"没有叫 {args.name} 的记忆", file=sys.stderr)
        return 1
    palace.save()
    print(f"{node.name} → {scope_label(node.scope)}（换项目也会记得它）")
    return 0


def cmd_projects(args) -> int:
    """列出记忆分布在哪些作用域，以及当前项目是哪个。"""
    palace = open_palace(resolve_store(args.store), getattr(args, "project", None))
    counts = palace.scope_counts()
    print(f"当前项目 {scope_label(palace.project)}")
    if not counts:
        print("（还没有任何记忆）")
        return 0
    total = sum(counts.values())
    print()
    for scope, count in counts.items():
        mark = " ←当前" if scope == palace.project else ("  ·共享" if scope == GLOBAL else "")
        bar = "█" * max(1, round(count / max(counts.values()) * 18))
        print(f"{bar:<18} {count:>3}  {scope_label(scope)}{mark}")
    print(f"{'':<18} {total:>3}  合计")
    return 0


def cmd_explain(args) -> int:
    path = resolve_store(args.store)
    palace = open_palace(path, getattr(args, "project", None))
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
    palace = open_palace(path, getattr(args, "project", None))
    node = palace.feel_outcome(args.name, outcome)
    if node is None:
        print(f"没有这条记忆：{args.name}", file=sys.stderr)
        return 1
    palace.save()
    print(f"{node.name}｜{_fmt_affect(node)}｜依据 {node.affect.drivers}")
    return 0


def cmd_pin(args) -> int:
    path = resolve_store(args.store)
    palace = open_palace(path, getattr(args, "project", None))
    node = palace.pin(args.name, pinned=not args.unpin)
    if node is None:
        print(f"没有这条记忆：{args.name}", file=sys.stderr)
        return 1
    palace.save()
    print(f"{node.name}｜{'已钉死' if node.pinned else '已解钉'}")
    return 0


def cmd_forget(args) -> int:
    path = resolve_store(args.store)
    palace = open_palace(path, getattr(args, "project", None))
    if not palace.drop(args.name, reason=args.reason):
        print(f"没有这条记忆：{args.name}", file=sys.stderr)
        return 1
    palace.save()
    print(f"已丢弃 {args.name}（进墓碑，可用代码 restore 复活）")
    return 0


def _slash(path: str) -> str:
    """配置里统一用正斜杠：YAML/TOML 的双引号字符串会把反斜杠当转义符。"""
    return path.replace("\\", "/")


def _paths() -> tuple[str, str, str]:
    """打印配置时要给绝对路径：宿主的工作目录不是仓库根，相对路径必然踩坑。"""
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return sys.executable, repo, resolve_store(None)


def _print_host_config(args) -> int:
    """打印可直接粘贴的宿主配置。绝对路径都算好了，不用手改。"""
    python, repo, store = _paths()
    if args.for_ == "dsh":
        print(f"""# DSH（DeepSeek Harness）· ~/.dsh/profiles/<profile>/cordis.patch.yml
# 保存后约 1 秒热重载；工具名会变成 mcp__memory-palace__memory_context
- insert:
    - id: mcp-memory-palace
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: memory-palace
        transport: stdio
        command: "{_slash(python)}"
        args: ["-u", "-m", "memory_palace.mcp_server", "--profile", "{args.profile}"]
        cwd: "{_slash(repo)}"
        env:
          MEMORY_PALACE_STORE: "{_slash(store)}"
          PYTHONPATH: "{_slash(repo)}"
        toolCallTimeoutMs: 30000
# 注意：DSH 启动子进程前会剥掉名字含 KEY/PASSWORD/SECRET/TOKEN/DSH_* 的环境变量，
# 所以存储路径必须显式写进 env，别指望从 shell 继承。""")
    elif args.for_ == "claude":
        print(f"""// Claude Code · 项目根 .mcp.json（或 claude mcp add）
{{
  "mcpServers": {{
    "memory-palace": {{
      "command": {json.dumps(python, ensure_ascii=False)},
      "args": ["-m", "memory_palace.mcp_server", "--profile", "{args.profile}"],
      "env": {{ "MEMORY_PALACE_STORE": {json.dumps(store, ensure_ascii=False)}, "PYTHONPATH": {json.dumps(repo, ensure_ascii=False)} }}
    }}
  }}
}}""")
    elif args.for_ == "codex":
        print(f"""# Codex CLI · ~/.codex/config.toml
[mcp_servers.memory-palace]
command = "{_slash(python)}"
args = ["-m", "memory_palace.mcp_server", "--profile", "{args.profile}"]
env = {{ MEMORY_PALACE_STORE = "{_slash(store)}", PYTHONPATH = "{_slash(repo)}" }}""")
    elif args.for_ == "cursor":
        print(f"""// Cursor · ~/.cursor/mcp.json
{{
  "mcpServers": {{
    "memory-palace": {{
      "command": {json.dumps(python, ensure_ascii=False)},
      "args": ["-m", "memory_palace.mcp_server", "--profile", "{args.profile}"],
      "env": {{ "MEMORY_PALACE_STORE": {json.dumps(store, ensure_ascii=False)}, "PYTHONPATH": {json.dumps(repo, ensure_ascii=False)} }}
    }}
  }}
}}""")
    else:  # http
        print(f"""# 任意支持 OpenAPI 的平台（Dify / Coze / n8n / GPT Actions）
# 1) 起服务（默认只绑 127.0.0.1，无鉴权）
"{python}" -m memory_palace.http_api --port 8765
# 2) 导入 OpenAPI
http://127.0.0.1:8765/openapi.json
# 3) 记忆库位置
MEMORY_PALACE_STORE={_slash(store)}""")
    return 0


def cmd_style(args) -> int:
    path = resolve_store(args.store)
    palace = open_palace(path, getattr(args, "project", None))
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


# ---------------------------------------------------------------- 运维命令


def _store_or_die(path: str):
    from .store import open_store

    store = open_store(path, backend=_DEFAULT_BACKEND)
    return store


def cmd_fsck(args) -> int:
    path = resolve_store(args.store)
    store = _store_or_die(path)
    report = store.fsck()
    if args.repair and not report.get("ok"):
        repaired = store.repair()
        report["repair"] = repaired
        report = store.fsck() if repaired.get("ok") else report
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report.get("ok") else 1
    print(f"库: {path}")
    print(f"后端: {report.get('backend')}  ok={report.get('ok')}")
    for key in (
        "revision", "nodes", "edges", "events", "commits", "snapshots",
        "journal_mode", "integrity", "orphan_edges", "wal_bytes", "bytes",
    ):
        if key in report:
            print(f"  {key:<12} {report[key]}")
    journal = report.get("journal") or {}
    if journal and journal.get("records") is not None:
        print(f"  日志记录     {journal['records']}（尾部残缺 {journal.get('tail_bytes', 0)} 字节）")
    lock = report.get("lock") or {}
    if lock:
        print(f"  当前锁持有者  pid={lock.get('pid')} {lock.get('label', '')}")
    if report.get("error"):
        print(f"  问题: {report['error']}")
    if report.get("repair"):
        print(f"  已修复: {report['repair']}")
    return 0 if report.get("ok") else 1


def cmd_journal(args) -> int:
    path = resolve_store(args.store)
    store = _store_or_die(path)
    rows = store.history(limit=args.limit)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print("还没有提交记录（空库）")
        return 0
    print(f"{'rev':>6}  {'时间':<19}  {'节点':>4}  {'原因':<12} 写者")
    for row in rows:
        when = datetime.fromtimestamp(row.get("ts") or 0).strftime("%Y-%m-%d %H:%M:%S")
        print(
            f"{row.get('rev', 0):>6}  {when:<19}  {row.get('nodes_written', 0):>4}  "
            f"{str(row.get('reason') or ''):<12} {row.get('writer') or ''}"
        )
    return 0


def cmd_lock(args) -> int:
    from .locking import holder_of

    path = resolve_store(args.store)
    info = holder_of(path + ".lock") or holder_of(path)
    if not info:
        print("没有进程持有写锁（或后端不使用锁文件，例如 SQLite 用自身事务锁）")
        return 0
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0


def cmd_export(args) -> int:
    path = resolve_store(args.store)
    store = _store_or_die(path)
    result = store.export(args.dest)
    print(f"已导出 {result['bytes']} 字节 → {result['dest']}")
    print(f"revision {result['revision']} · sha256 {result['sha256'][:16]}…")
    return 0


def cmd_import(args) -> int:
    path = resolve_store(args.store)
    store = _store_or_die(path)
    result = store.import_(args.src)
    print(f"已导入 → {path}（revision {result['revision']}）")
    if result.get("backup"):
        print(f"导入前的库已备份为 {result['backup']}")
    return 0


def cmd_embed(args) -> int:
    path = resolve_store(args.store)
    palace = open_palace(path, getattr(args, "project", None))
    cfg = palace._config
    if args.reindex:
        palace.semantics.mark_dirty()
    # 状态要如实反映「现在」：没有 sync 之前索引是脏的，报出来的向量数会是 0
    palace.semantics.sync(palace.graph)
    detail = palace.semantics.explain(args.query or "仓位与资金接力")
    channels = detail.get("channels", {})
    print(f"后端存储: {palace._store.describe().get('backend') if palace._store else '内存'}")
    print(f"词法通道: 启用（{len(palace.semantics)} 条向量）")
    print(f"稠密通道: {'启用' if channels.get('dense') else '未启用'}"
          f"  嵌入器={channels.get('embedder') or '（无）'}")
    print(f"  稠密向量数 {channels.get('dense_vectors', 0)} · 失败 {channels.get('dense_failures', 0)}")
    print(f"  混合权重 {channels.get('hybrid_weight')}")
    print(f"配置: provider={cfg.embedding_provider or '(空)'} dim={cfg.embedding_dim}")
    if not channels.get("dense"):
        print("\n启用方式：memory-palace config --set embedding_provider=hashing（零依赖）")
        print("        或 embedding_provider=http + embedding_endpoint=...（接 BGE/vLLM/云端）")
    return 0


def cmd_eval(args) -> int:
    from . import evaluation as ev

    names = [args.dataset] if args.dataset else None
    embedder = None
    if args.channel in ("hybrid", "dense"):
        from .embedding import HashingEmbedder

        embedder = HashingEmbedder(512)
    report = ev.run_suite(names, k=args.k, embedder=embedder, chunk=args.channel)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(ev.format_report(report))
    if args.scale:
        print("\n规模测试（合成语料）")
        print(f"{'节点':>7}  {'重建ms':>8}  {'p50查询ms':>9}  {'p95查询ms':>9}  recall@5")
        for row in ev.scale_benchmark(tuple(args.scale)):
            print(
                f"{row['nodes']:>7}  {row['rebuild_ms']:>8}  {row['p50_query_ms']:>9}  "
                f"{row['p95_query_ms']:>9}  {row['recall_at_5']}"
            )
    return 0


def cmd_calibrate(args) -> int:
    from . import evaluation as ev

    names = [args.dataset] if args.dataset else None
    result = ev.calibrate(names)
    print(json.dumps({k: v for k, v in result.items() if k != "curves"}, ensure_ascii=False, indent=2))
    if args.apply:
        target = os.path.join(os.path.dirname(resolve_store(args.store)), "calibration.json")
        payload = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "datasets": result["datasets"],
            "dedupe_similarity": result["dedupe"]["recommended"],
            "abstain_semantic_floor": result["abstain"]["recommended"],
        }
        with open(target, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"\n已写入 {target}")
        print("用法：memory-palace config --calibration 或在代码里 PalaceConfig.from_calibration(path)")
    return 0


def cmd_reflect(args) -> int:
    path = resolve_store(args.store)
    palace = open_palace(path, getattr(args, "project", None))
    report = palace.reflect()
    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        return 0
    for finding in report.findings:
        print(f"- {finding}")
    print()
    print(report.narrative)
    if palace.reflection.reflector_error:
        print(f"\n外部反思失败（已降级为规则反思）：{palace.reflection.reflector_error}")
    if palace.llm is not None and palace.llm.last is not None:
        print(f"\nLLM 反思：provider={palace.llm.last.provider} "
              f"confidence={palace.llm.last.confidence:.2f} "
              f"耗时={palace.llm.last.elapsed_ms:.0f}ms")
        for item in palace.llm.last.suggested_params:
            print(f"  建议 {item['param']} 向 {item['direction']}（未生效，需过护栏）")
    return 0


def cmd_stats(args) -> int:
    from .core import WRITE_STATS

    path = resolve_store(args.store)
    store = _store_or_die(path)
    if hasattr(store, "stats_report"):
        data = store.stats_report()
    else:
        data = store.describe()
        data["history"] = store.history(limit=5)
    data["write_stats"] = dict(WRITE_STATS)
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


def cmd_config(args) -> int:
    """config 有两个职责：打印宿主配置（--for），以及改库里的参数（--set）。

    改参数必须落到快照里才有意义 —— 只改内存等于「这次运行有效」，
    而用户说的是「以后都这样」。所以这里强制 save 一次。
    """
    if getattr(args, "set", None):
        path = resolve_store(args.store)
        key, _, raw = args.set.partition("=")
        if not _:
            print("用法：--set key=value", file=sys.stderr)
            return 2
        palace = open_palace(path, getattr(args, "project", None), autosave=False)
        cfg = palace._config
        if not hasattr(cfg, key):
            print(f"没有这个配置项: {key}", file=sys.stderr)
            return 2
        current = getattr(cfg, key)
        try:
            if isinstance(current, bool):
                value: Any = raw.lower() in ("1", "true", "yes", "on")
            elif isinstance(current, int):
                value = int(raw)
            elif isinstance(current, float):
                value = float(raw)
            else:
                value = raw
        except ValueError:
            print(f"值类型不对: {key}={raw!r}", file=sys.stderr)
            return 2
        setattr(cfg, key, value)
        cfg.validate()
        palace.save(reason="config")
        print(f"已设置 {key} = {value!r}（写入快照）")
        return 0
    return _print_host_config(args)


# ---------------------------------------------------------------- 入口


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--store", help=f"记忆库路径（默认 {DEFAULT_STORE}）")
    common.add_argument(
        "--backend",
        default="auto",
        choices=["auto", "sqlite", "json"],
        help="存储后端：sqlite（默认，WAL 并发）/ json（单文件可移植）/ auto（按扩展名）",
    )
    common.add_argument(
        "--project",
        help="当前项目标识（默认按 git remote / 目录名自动推断；决定默认写哪里、读哪些）",
    )

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
    p.add_argument("--supersedes", action="append", metavar="NAME",
                   help="被本条取代的旧记忆名（旧的不删，而是到期，可 as-of 回看）")
    p.add_argument("--no-merge", action="store_true",
                   help="关闭去重：内容几乎相同时默认合并到已有条目，而不是新开一条")
    p.add_argument("--scope", default="",
                   help="写到哪里：global=跨项目共享 / project=当前项目（默认） / session=仅本次会话")
    p.add_argument("--auto-tier", action="store_true",
                   help="按语用判断建议层级（禁令进 core、暗示只进 event）；不给就沿用 --tier")
    p.set_defaults(func=cmd_remember)

    p = sub.add_parser("recall", parents=[common], help="检索（语义 / 名字）")
    p.add_argument("query", nargs="?", help="语义查询")
    p.add_argument("--as-of", dest="as_of",
                   help="回看某个时刻当时为真的记忆（30d 或 2026-10-01）")
    p.add_argument("--scope", default="auto", choices=list(SCOPE_MODES),
                   help="auto（默认）/ global / project / session / all")
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
    p.add_argument("--scope", default="auto", choices=list(SCOPE_MODES),
                   help="看哪些记忆：auto=全局+当前项目（默认）/ global=只要跨项目 / project=只要本项目 / all=全部")
    p.add_argument("--tree", action="store_true", help="改用树状注入（根常驻、枝按需展开、其余折叠）")
    p.add_argument("--expand", type=int, default=1, help="树状模式下展开几个枝（默认 1）")
    p.add_argument("--compare", action="store_true", help="同时算出扁平注入的用量，报告省了多少字")
    p.set_defaults(func=cmd_context)

    p = sub.add_parser("view", parents=[common], help="终端视图：谁重要、谁快被忘掉")
    p.add_argument("--scope", default="auto", choices=list(SCOPE_MODES),
                   help="auto（默认）/ global / project / all")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--days", type=float, default=30, help="寿命预警窗口（天）")
    p.set_defaults(func=cmd_view)

    p = sub.add_parser("render", parents=[common], help="生成自包含 HTML 记忆面板（含衰减曲线）")
    p.add_argument("--scope", default="auto", choices=list(SCOPE_MODES),
                   help="auto（默认）/ global / project / all")
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

    p = sub.add_parser("promote", parents=[common], help="把一条记忆升为跨项目（global）")
    p.add_argument("name", help="记忆名")
    p.add_argument("--to", default="global", help="目标作用域（默认 global）")
    p.set_defaults(func=cmd_promote)

    p = sub.add_parser("projects", parents=[common], help="记忆分布在哪些作用域")
    p.set_defaults(func=cmd_projects)

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

    p = sub.add_parser("config", parents=[common], help="打印宿主配置（dsh / claude / codex / cursor / http）")
    p.add_argument("--for", dest="for_", default="dsh",
                   choices=["dsh", "claude", "codex", "cursor", "http"])
    p.add_argument("--profile", default="slim", choices=["slim", "full"],
                   help="工具档位：slim 三个工具（省 token）/ full 八个")
    p.add_argument("--set", metavar="KEY=VALUE", help="改库里的参数并写回快照，例如 --set hybrid_weight=0.5")
    p.set_defaults(func=cmd_config)

    p = sub.add_parser("style", parents=[common], help="风格证据与生效状态")
    p.add_argument("action", choices=["observe", "active"])
    p.add_argument("key", nargs="?", help="风格键（observe 时必填）")
    p.add_argument("statement", nargs="?", help="风格描述（observe 时必填）")
    p.add_argument("--sign", type=int, default=1, help="+1 支持 / -1 反证")
    p.add_argument("--opposes", help="与哪些风格键对立（逗号分隔）")
    p.set_defaults(func=cmd_style)

    p = sub.add_parser("fsck", parents=[common], help="体检：校验和 / 完整性 / 悬空边 / 锁持有者")
    p.add_argument("--repair", action="store_true", help="尝试修复（先备份，不删数据）")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_fsck)

    p = sub.add_parser("journal", parents=[common], help="提交账本：每次写入的时间、原因、写者")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_journal)

    p = sub.add_parser("lock", parents=[common], help="谁持有写锁")
    p.set_defaults(func=cmd_lock)

    p = sub.add_parser("export", parents=[common], help="导出一份可搬走的完整档案")
    p.add_argument("dest", help="输出文件路径")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("import", parents=[common], help="从档案导入（导入前自动备份现有库）")
    p.add_argument("src", help="档案文件路径")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("embed", parents=[common], help="嵌入通道状态（词法 / 稠密 / 混合权重）")
    p.add_argument("--query", help="用这条查询看通道解释")
    p.add_argument("--reindex", action="store_true", help="强制重建语义索引")
    p.set_defaults(func=cmd_embed)

    p = sub.add_parser("eval", parents=[common], help="组件级评测：检索 / 弃答 / 去重 / 沉淀")
    p.add_argument("--dataset", choices=["zh-finance", "zh-tech"], help="只跑一个数据集")
    p.add_argument("--channel", default="lexical", choices=["lexical", "hybrid", "dense"])
    p.add_argument("-k", type=int, default=5)
    p.add_argument("--json", action="store_true")
    p.add_argument("--scale", nargs="*", type=int, metavar="N",
                   help="额外跑规模测试，例如 --scale 1000 5000")
    p.set_defaults(func=cmd_eval)

    p = sub.add_parser("calibrate", parents=[common], help="在标注集上扫阈值，给推荐值与平台区间")
    p.add_argument("--dataset", choices=["zh-finance", "zh-tech"])
    p.add_argument("--apply", action="store_true", help="把推荐值写成 calibration.json")
    p.set_defaults(func=cmd_calibrate)

    p = sub.add_parser("reflect", parents=[common], help="跑一次反思（配了 LLM 就用 LLM）")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_reflect)

    p = sub.add_parser("stats", parents=[common], help="存储与写入统计（JSON）")
    p.set_defaults(func=cmd_stats)

    return parser



def main(argv: list[str] | None = None) -> int:
    ensure_utf8_console()
    parser = build_parser()
    args = parser.parse_args(argv)
    global _DEFAULT_BACKEND
    _DEFAULT_BACKEND = getattr(args, "backend", "auto")
    if args.command == "style" and args.action == "observe" and not (args.key and args.statement):
        parser.error("style observe 需要 key 与 statement")
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
