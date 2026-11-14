"""mcp_server.py - 把记忆系统暴露成 MCP 服务（stdio，零第三方依赖）。

为什么是 MCP：2026 年主流 agent（Claude Code、Codex CLI、Cursor、Cline、Windsurf、
Zed、Gemini CLI、Continue…）几乎都支持它。写一次，全都能接；而 skill 只能服务一个宿主。

传输：stdio，一行一个 JSON-RPC 2.0 消息（MCP 的 stdio 传输就是换行分隔的 JSON，没有
Content-Length 头 —— 这点和 LSP 不同，很容易写错，所以单独说明）。

用法（由 agent 宿主启动，不是人手动跑）：
    python -m memory_palace.mcp_server
    memory-palace-mcp            # pip install 之后

存储：环境变量 MEMORY_PALACE_STORE，默认 ~/.memory-palace/store.json。
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import traceback
from typing import Any, Callable

from .cli import DEFAULT_STORE, build_context, format_context, open_palace, parse_tags

SERVER_INFO = {"name": "memory-palace", "version": "0.5.0"}
PROTOCOL_DEFAULT = "2024-11-05"

# 工具描述是写给模型看的：说清「什么时候该用」，比说清参数更重要。
TOOLS: list[dict[str, Any]] = [
    {
        "name": "memory_context",
        "description": (
            "取一块受预算约束的长期记忆上下文，用于塞进本轮 system prompt。"
            "在任何需要延续性的任务开始前调用（复盘、报价、写作风格、项目约定）。"
            "钉死的硬规则总会包含在内；带「细节可能失真」标记的条目，引用细节时要说明不确定。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "本轮任务的一句话描述；省略则按当前显著度"},
                "budget": {"type": "integer", "description": "字符预算，默认 900", "default": 900},
                "limit": {"type": "integer", "description": "最多几条，默认 6", "default": 6},
                "names": {"type": "array", "items": {"type": "string"}, "description": "必须包含的记忆名"},
                "scope": {
                    "type": "string",
                    "enum": ["auto", "global", "project", "all"],
                    "default": "auto",
                    "description": (
                        "看哪些记忆：auto=全局+当前项目（默认）/ global=只要跨项目共享的 / "
                        "project=只要本项目的 / all=全部"
                    ),
                },
                "tree": {
                    "type": "boolean",
                    "description": "用树状注入（根常驻、枝按需展开、其余折叠）。记忆多于 ~30 条时更省 token",
                    "default": False,
                },
            },
        },
    },
    {
        "name": "memory_remember",
        "description": (
            "记住一条**结论或教训**（不是过程、不是原文）。用户说「记住」「以后都这样」时调用。"
            "稳定偏好用 tier=fact；硬规则加 pin=true。返回记忆名，后续反馈要用它。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "一句话结论"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "tier": {"type": "string", "enum": ["trace", "ephemeral", "event", "fact", "core"], "default": "event"},
                "pin": {"type": "boolean", "description": "钉死：不衰减、不参与自动遗忘（只给真正的硬规则）"},
                "name": {"type": "string"},
                "valence": {"type": "number", "description": "效价 [-1,1]，省略则由文本推断"},
                "arousal": {"type": "number", "description": "唤醒 [0,1]，省略则由文本推断"},
                "scope": {
                    "type": "string",
                    "description": (
                        "写到哪里：global=跨项目共享（用户偏好/硬规则/通用教训）/ "
                        "project=本项目（默认）/ session=仅本次会话"
                    ),
                },
                "auto_tier": {
                    "type": "boolean",
                    "default": True,
                    "description": "按语用判断建议层级（禁令 core / 暗示 event）。关掉则用 tier 原值",
                },
                "supersedes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "被这条取代的旧记忆名。用户改主意时**务必**填（例如「以后用短段落」取代「用表格」）。"
                        "旧的不会被删，而是标注失效，仍可用 as_of 回看。"
                    ),
                },
            },
            "required": ["content"],
        },
    },
    {
        "name": "memory_recall",
        "description": "按语义或名字检索记忆，返回名字、内容与重要性拆解。需要精确查看某条时用。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "names": {"type": "array", "items": {"type": "string"}},
                "limit": {"type": "integer", "default": 6},
                "as_of": {
                    "type": "string",
                    "description": "回看某个时刻当时为真的记忆（30d 或 2026-10-01）。问「以前是怎么定的」时用",
                },
            },
        },
    },
    {
        "name": "memory_promote",
        "description": (
            "把一条项目记忆升为跨项目共享。当某个项目的教训其实是通用的"
            "（例如「上线前必须先跑迁移」），用它让下个项目也记得。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "记忆名"},
                "to": {"type": "string", "default": "global", "description": "目标作用域，默认 global"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "memory_outcome",
        "description": (
            "把一次结果反馈成情绪证据：用户纠正你 → contradicted；用户认可 → confirmed；"
            "止损/亏损 → loss。只影响「这条还可不可信」，不强化记忆。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "result": {
                    "type": "string",
                    "enum": ["loss", "win", "error", "contradicted", "confirmed", "missed", "recalled"],
                },
            },
            "required": ["name", "result"],
        },
    },
    {
        "name": "memory_forget",
        "description": "显式丢弃一条记忆（进墓碑）。用户说「忘掉这个」时用；不要用文件编辑代替。",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "reason": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "memory_status",
        "description": "记忆库体检：规模、价值/结构健康、生效风格、自修正状态。用户问「你记得什么」时先看它。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "memory_explain",
        "description": "解释一次检索：命中哪些概念、多义词归给了谁、逐条打分。用户问「你怎么想起这个」时用。",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "name": "memory_cycle",
        "description": (
            "跑一轮记忆代谢：沉淀碎片成结论、按价值遗忘、更新自述、反思。"
            "在长任务收尾时调用一次即可，不要每轮都调。"
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
]


# ---------------------------------------------------------------- 工具实现


def _store() -> str:
    return os.path.abspath(os.path.expanduser(os.environ.get("MEMORY_PALACE_STORE") or DEFAULT_STORE))


def _text(value: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": value}], "isError": False}


def _fail(message: str) -> dict[str, Any]:
    # 工具级失败用 isError，而不是 JSON-RPC error：让模型看得见并自行纠正
    return {"content": [{"type": "text", "text": message}], "isError": True}


def tool_context(args: dict[str, Any]) -> dict[str, Any]:
    from .tree import build_tree, render_tree

    palace = open_palace(_store())
    query = args.get("query")
    budget = int(args.get("budget", 900))
    if args.get("tree"):
        scores = (
            {r["name"]: r["score"] for r in palace.score_breakdown(query)} if query else {}
        )
        rendered = render_tree(
            build_tree(palace, scores=scores),
            expand=int(args.get("expand", 1)),
            budget=budget,
            leaf_limit=max(1, int(args.get("limit", 6)) // 2),
        )
        return _text(rendered["text"])
    ctx = build_context(
        palace,
        query=query,
        limit=int(args.get("limit", 6)),
        budget=budget,
        names=args.get("names") or None,
        scope_mode=str(args.get("scope") or "auto"),
    )
    return _text(format_context(ctx))


def tool_remember(args: dict[str, Any]) -> dict[str, Any]:
    content = str(args.get("content", "")).strip()
    if not content:
        return _fail("content 不能为空")
    palace = open_palace(_store())
    name = args.get("name") or "m:" + hashlib.blake2b(content.encode("utf-8"), digest_size=4).hexdigest()
    if name in palace.graph:
        return _text(f"已存在同名记忆 {name}（内容未变则不重复写入）")
    node = palace.remember(
        name,
        content,
        tags=parse_tags(",".join(args.get("tags") or [])),
        tier=str(args.get("tier", "event")),
        pinned=bool(args.get("pin", False)),
        valence=args.get("valence"),
        arousal=args.get("arousal"),
        supersedes=list(args.get("supersedes") or []),
        scope=str(args.get("scope") or ""),
        # agent 入口默认按语用建议分层：禁令进 core、暗示只进 event。
        # 这是这一层对 agent 最实际的价值 —— 它不该把「随便你」记成偏好。
        auto_tier=bool(args.get("auto_tier", True)),
        # agent 入口默认去重：同一句话反复记，不该攒成一堆近义节点
        merge=True,
    )
    palace.save()
    extra = ""
    if node.supersedes:
        extra = f"｜取代 {', '.join(node.supersedes)}（仍可 as_of 回看）"
    return _text(
        f"已记住 {node.name}｜情绪 v={node.affect.valence:+.2f}/a={node.affect.arousal:.2f}"
        f"（{node.affect.source}）｜层级 {node.tier}{extra}"
    )


def tool_recall(args: dict[str, Any]) -> dict[str, Any]:
    from .cli import parse_moment

    palace = open_palace(_store())
    as_of = parse_moment(args.get("as_of"), palace.now())
    hits = palace.recall(
        names=args.get("names") or None,
        query=args.get("query"),
        limit=int(args.get("limit", 6)),
        as_of=as_of,
    )
    if not hits:
        return _text("（没有命中）")
    rows = [
        {
            "name": n.name,
            "content": n.content,
            "tags": list(n.tags),
            "importance": palace.importance_of(n.name),
            **(
                {"retired": True, "superseded_by": n.superseded_by}
                if n.is_retired() else {}
            ),
        }
        for n in hits
    ]
    return _text(json.dumps(rows, ensure_ascii=False, indent=2))


def tool_promote(args: dict[str, Any]) -> dict[str, Any]:
    palace = open_palace(_store())
    node = palace.promote(str(args.get("name", "")), str(args.get("to") or "global"))
    if node is None:
        return _fail("没有这条记忆")
    palace.save()
    return _text(f"{node.name} 已提升为 {node.scope}：换项目也会记得它")


def tool_outcome(args: dict[str, Any]) -> dict[str, Any]:
    from .cli import OUTCOME_ALIASES

    result = OUTCOME_ALIASES.get(str(args.get("result", "")))
    if result is None:
        return _fail(f"未知结果类型：{args.get('result')!r}")
    palace = open_palace(_store())
    node = palace.feel_outcome(str(args["name"]), result)
    if node is None:
        return _fail(f"没有这条记忆：{args['name']}（名字可能已过期，先 recall）")
    palace.save()
    return _text(f"{node.name}｜v={node.affect.valence:+.2f}/a={node.affect.arousal:.2f}｜{node.affect.drivers}")


def tool_forget(args: dict[str, Any]) -> dict[str, Any]:
    palace = open_palace(_store())
    if not palace.drop(str(args["name"]), reason=str(args.get("reason", "agent_requested"))):
        return _fail(f"没有这条记忆：{args['name']}")
    palace.save()
    return _text(f"已丢弃 {args['name']}")


def tool_status(_args: dict[str, Any]) -> dict[str, Any]:
    palace = open_palace(_store())
    snap = palace.snapshot()
    brief = {
        "store": _store(),
        "nodes": snap["nodes"],
        "edges": snap["edges"],
        "tiers": snap["tiers"],
        "entropy_health": snap["entropy_health"],
        "structure_health": snap["structure_health"],
        "active_style": snap["active_style"],
        "selfcorrect": {
            "pending": len(snap["selfcorrect"]["pending"]),
            "blocked": snap["selfcorrect"]["blocked"],
        },
        "narrative_rev": snap["self_model_revision"],
    }
    return _text(json.dumps(brief, ensure_ascii=False, indent=2))


def tool_explain(args: dict[str, Any]) -> dict[str, Any]:
    palace = open_palace(_store())
    detail = palace.semantic_explain(str(args["query"]))
    rows = palace.score_breakdown(str(args["query"]))[:3]
    detail["scores"] = rows
    return _text(json.dumps(detail, ensure_ascii=False, indent=2))


def tool_cycle(_args: dict[str, Any]) -> dict[str, Any]:
    palace = open_palace(_store())
    report = palace.run_cycle()
    palace.save()
    return _text(
        f"节点 {report['nodes']}｜沉淀 {len(report['consolidated'])}｜"
        f"遗忘 {len(report['forgotten'])}｜自述第 {report['narrative']['rev']} 版\n"
        f"{report['narrative']['text']}"
    )


# 工具档位：DSH 这类宿主会把**全部工具定义**常驻进上下文（不支持 Tool Search），
# 8 个工具的定义≈1.2k token，每轮都付。slim 只留三个动词：取、记、代谢。
PROFILES: dict[str, tuple[str, ...]] = {
    "slim": ("memory_context", "memory_remember", "memory_cycle"),
    "full": tuple(t["name"] for t in TOOLS),
}
ACTIVE: set[str] = set(PROFILES["full"])


def set_profile(name: str) -> None:
    global ACTIVE
    if name not in PROFILES:
        raise SystemExit(f"未知 profile: {name}（可选 {sorted(PROFILES)}）")
    ACTIVE = set(PROFILES[name])


HANDLERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "memory_context": tool_context,
    "memory_remember": tool_remember,
    "memory_recall": tool_recall,
    "memory_promote": tool_promote,
    "memory_outcome": tool_outcome,
    "memory_forget": tool_forget,
    "memory_status": tool_status,
    "memory_explain": tool_explain,
    "memory_cycle": tool_cycle,
}


# ---------------------------------------------------------------- JSON-RPC


def handle(request: dict[str, Any]) -> dict[str, Any] | None:
    method = request.get("method")
    request_id = request.get("id")
    is_notification = "id" not in request

    if method == "initialize":
        requested = (request.get("params") or {}).get("protocolVersion")
        result = {
            "protocolVersion": requested or PROTOCOL_DEFAULT,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
        }
        return None if is_notification else {"jsonrpc": "2.0", "id": request_id, "result": result}

    if method in ("notifications/initialized", "initialized"):
        return None  # 通知不需要回复

    if method == "ping":
        return None if is_notification else {"jsonrpc": "2.0", "id": request_id, "result": {}}

    if method == "tools/list":
        visible = [t for t in TOOLS if t["name"] in ACTIVE]
        return (
            None
            if is_notification
            else {"jsonrpc": "2.0", "id": request_id, "result": {"tools": visible}}
        )

    if method == "tools/call":
        params = request.get("params") or {}
        name = params.get("name")
        handler = HANDLERS.get(str(name))
        if handler is not None and str(name) not in ACTIVE:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {
                    "code": -32601,
                    "message": f"当前 profile 未启用 {name}（用 --profile full 启动）",
                },
            }
        if handler is None:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": f"未知工具：{name}"},
            }
        try:
            result = handler(params.get("arguments") or {})
        except Exception as exc:  # noqa: BLE001 - 工具内部错误要回给模型，不能拖垮整个服务
            result = _fail(f"{type(exc).__name__}: {exc}")
            print(traceback.format_exc(), file=sys.stderr)
        return None if is_notification else {"jsonrpc": "2.0", "id": request_id, "result": result}

    if is_notification:
        return None
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": f"未实现的方法：{method}"},
    }


def main(argv: list[str] | None = None) -> int:
    """stdio 主循环：一行一条 JSON-RPC。EOF 即退出。

    `--profile slim` 只暴露 context/remember/cycle 三个工具，给「工具定义常驻上下文」
    的宿主（DSH 等）省 token；默认 full。
    """
    import argparse

    parser = argparse.ArgumentParser(prog="memory-palace-mcp", description="MCP 记忆服务（stdio）")
    parser.add_argument("--profile", choices=sorted(PROFILES), default="full")
    parser.add_argument("--list-tools", action="store_true", help="打印工具清单后退出（排障用）")
    ns = parser.parse_args(argv)
    set_profile(ns.profile)
    if ns.list_tools:
        for tool in TOOLS:
            mark = "on " if tool["name"] in ACTIVE else "off"
            print(f"[{mark}] {tool['name']}: {tool['description'].splitlines()[0][:60]}")
        return 0
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            print(json.dumps({"jsonrpc": "2.0", "id": None,
                              "error": {"code": -32700, "message": "Parse error"}}), flush=True)
            continue
        try:
            response = handle(request)
        except Exception as exc:  # noqa: BLE001
            print(traceback.format_exc(), file=sys.stderr)
            response = {
                "jsonrpc": "2.0",
                "id": request.get("id"),
                "error": {"code": -32603, "message": f"Internal error: {exc}"},
            }
        if response is not None:
            print(json.dumps(response, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
