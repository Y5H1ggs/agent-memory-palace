"""http_api.py - 把记忆系统暴露成本地 HTTP 服务（含 OpenAPI 3.0，零第三方依赖）。

为什么在 MCP 之外还要 HTTP：MCP 覆盖了「有 MCP 客户端的 agent」，但 Dify / Coze / n8n /
自定义 GPT Actions / 各种低代码平台只认 HTTP + OpenAPI。两条腿才能覆盖主流。

安全：默认只绑 127.0.0.1，没有鉴权。要跨机访问请自己套反向代理加 token，
不要直接把 `--allow-remote` 暴露在公网 —— 这是一个能读你全部记忆的接口。

用法：
    python -m memory_palace.http_api --port 8765
    memory-palace-http --port 8765

    curl -s -X POST localhost:8765/context -d '{"query":"仓位怎么分配"}'
    curl -s localhost:8765/openapi.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .cli import DEFAULT_STORE, build_context, format_context, open_palace, parse_tags

MAX_BODY = 1 << 20  # 1MB，防手滑


def _store() -> str:
    return os.path.abspath(os.path.expanduser(os.environ.get("MEMORY_PALACE_STORE") or DEFAULT_STORE))


# ---------------------------------------------------------------- 处理函数


def api_context(body: dict[str, Any]) -> dict[str, Any]:
    palace = open_palace(_store())
    ctx = build_context(
        palace,
        query=body.get("query"),
        limit=int(body.get("limit", 6)),
        budget=int(body.get("budget", 900)),
        names=body.get("names") or None,
    )
    return {"text": format_context(ctx), **ctx}


def api_remember(body: dict[str, Any]) -> dict[str, Any]:
    content = str(body.get("content", "")).strip()
    if not content:
        raise ValueError("content 不能为空")
    palace = open_palace(_store())
    tags = body.get("tags") or []
    node = palace.remember(
        body.get("name") or "m:" + hashlib.blake2b(content.encode("utf-8"), digest_size=4).hexdigest(),
        content,
        tags=parse_tags(",".join(tags) if isinstance(tags, list) else str(tags)),
        tier=str(body.get("tier", "event")),
        pinned=bool(body.get("pin", False)),
        valence=body.get("valence"),
        arousal=body.get("arousal"),
        supersedes=list(body.get("supersedes") or []),
        merge=bool(body.get("merge", True)),
    )
    palace.save()
    return {
        "name": node.name,
        "tier": node.tier,
        "supersedes": list(node.supersedes),
        "affect": {"valence": round(node.affect.valence, 4),
                   "arousal": round(node.affect.arousal, 4),
                   "source": node.affect.source},
    }


def api_recall(body: dict[str, Any]) -> dict[str, Any]:
    from .cli import parse_moment

    palace = open_palace(_store())
    as_of = parse_moment(body.get("as_of"), palace.now())
    hits = palace.recall(
        names=body.get("names") or None,
        query=body.get("query"),
        limit=int(body.get("limit", 6)),
        as_of=as_of,
    )
    return {
        "as_of": as_of,
        "items": [
            {"name": n.name, "content": n.content, "tags": list(n.tags),
             "importance": palace.importance_of(n.name),
             **({"retired": True, "superseded_by": n.superseded_by} if n.is_retired() else {})}
            for n in hits
        ],
    }


def api_outcome(body: dict[str, Any]) -> dict[str, Any]:
    from .cli import OUTCOME_ALIASES

    result = OUTCOME_ALIASES.get(str(body.get("result", "")))
    if result is None:
        raise ValueError(f"未知结果类型：{body.get('result')!r}")
    palace = open_palace(_store())
    node = palace.feel_outcome(str(body.get("name", "")), result)
    if node is None:
        raise KeyError(f"没有这条记忆：{body.get('name')}")
    palace.save()
    return {"name": node.name, "valence": round(node.affect.valence, 4),
            "arousal": round(node.affect.arousal, 4), "drivers": list(node.affect.drivers)}


def api_status(_body: dict[str, Any]) -> dict[str, Any]:
    palace = open_palace(_store())
    snap = palace.snapshot()
    return {
        "store": _store(),
        "nodes": snap["nodes"],
        "edges": snap["edges"],
        "tiers": snap["tiers"],
        "entropy_health": snap["entropy_health"],
        "structure_health": snap["structure_health"],
        "active_style": snap["active_style"],
        "narrative_rev": snap["self_model_revision"],
    }


def api_cycle(_body: dict[str, Any]) -> dict[str, Any]:
    palace = open_palace(_store())
    report = palace.run_cycle()
    palace.save()
    return {
        "nodes": report["nodes"],
        "consolidated": report["consolidated"],
        "forgotten": report["forgotten"],
        "narrative": report["narrative"]["text"],
    }


ROUTES = {
    "/context": api_context,
    "/remember": api_remember,
    "/recall": api_recall,
    "/outcome": api_outcome,
    "/status": api_status,
    "/cycle": api_cycle,
}


# ---------------------------------------------------------------- OpenAPI


def openapi_spec(host: str, port: int) -> dict[str, Any]:
    def op(name: str, summary: str, props: dict[str, Any], required: list[str]) -> dict[str, Any]:
        return {
            "operationId": name,
            "summary": summary,
            "requestBody": {
                "required": True,
                "content": {"application/json": {"schema": {
                    "type": "object", "properties": props, "required": required}}},
            },
            "responses": {"200": {"description": "OK", "content": {"application/json": {"schema": {
                "type": "object"}}}}},
        }

    return {
        "openapi": "3.0.3",
        "info": {
            "title": "memory-palace",
            "version": "0.3.0",
            "description": "会遗忘、会被情绪加固、会讲自己故事的长期记忆。给 agent 用：context 注入、remember 固化、cycle 代谢。",
        },
        "servers": [{"url": f"http://{host}:{port}"}],
        "paths": {
            "/context": {"post": op("memory_context", "取一段受预算约束的记忆上下文", {
                "query": {"type": "string", "description": "本轮任务描述"},
                "budget": {"type": "integer", "default": 900},
                "limit": {"type": "integer", "default": 6},
                "names": {"type": "array", "items": {"type": "string"}},
            }, [])},
            "/remember": {"post": op("memory_remember", "记住一条结论或教训", {
                "content": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "tier": {"type": "string", "enum": ["trace", "ephemeral", "event", "fact", "core"]},
                "pin": {"type": "boolean"},
                "valence": {"type": "number"},
                "arousal": {"type": "number"},
                "supersedes": {"type": "array", "items": {"type": "string"},
                               "description": "被本条取代的旧记忆名（旧的标注失效，不删除）"},
                "merge": {"type": "boolean", "default": True,
                          "description": "内容几乎相同时合并到已有条目"},
            }, ["content"])},
            "/recall": {"post": op("memory_recall", "检索记忆", {
                "query": {"type": "string"},
                "names": {"type": "array", "items": {"type": "string"}},
                "limit": {"type": "integer", "default": 6},
                "as_of": {"type": "string",
                          "description": "回看某个时刻为真的记忆（30d 或 2026-10-01）"},
            }, [])},
            "/outcome": {"post": op("memory_outcome", "把结果反馈成情绪证据", {
                "name": {"type": "string"},
                "result": {"type": "string",
                           "enum": ["loss", "win", "error", "contradicted", "confirmed", "missed", "recalled"]},
            }, ["name", "result"])},
            "/status": {"post": op("memory_status", "记忆库体检", {}, [])},
            "/cycle": {"post": op("memory_cycle", "跑一轮记忆代谢", {}, [])},
        },
    }


# ---------------------------------------------------------------- HTTP


class Handler(BaseHTTPRequestHandler):
    server_version = "memory-palace/0.3.0"

    def log_message(self, fmt: str, *args: Any) -> None:  # 静音：stdout 留给 agent 读
        return

    def _send(self, code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, code: int, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") in ("/panel", "/panel.html", "/"):
            try:
                from .render import html_report

                palace = open_palace(_store())
                self._send_html(200, html_report(palace, scope="auto"))
            except Exception as exc:  # noqa: BLE001
                self._send_html(500, f"<pre>记忆面板生成失败：{exc}</pre>")
            return
        if self.path.rstrip("/") in ("/health",):
            try:
                palace = open_palace(_store())
                self._send(200, {"ok": True, "nodes": len(palace.graph), "store": _store()})
            except Exception as exc:  # noqa: BLE001
                self._send(500, {"ok": False, "error": str(exc)})
            return
        if self.path.rstrip("/") == "/openapi.json":
            host, port = self.server.server_address[:2]
            self._send(200, openapi_spec(str(host), int(port)))
            return
        self._send(404, {"error": "not found",
                         "paths": sorted(ROUTES) + ["/health", "/openapi.json", "/panel"]})

    def do_POST(self) -> None:  # noqa: N802
        route = self.path.rstrip("/")
        handler = ROUTES.get(route)
        if handler is None:
            self._send(404, {"error": f"未知路径 {self.path}", "paths": sorted(ROUTES)})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                self._send(413, {"error": "body too large"})
                return
            raw = self.rfile.read(length) if length else b"{}"
            body = json.loads(raw.decode("utf-8") or "{}")
            if not isinstance(body, dict):
                raise ValueError("body 必须是 JSON 对象")
            self._send(200, handler(body))
        except KeyError as exc:
            self._send(404, {"error": str(exc).strip("'\"")})
        except (ValueError, TypeError) as exc:
            self._send(400, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            self._send(500, {"error": f"{type(exc).__name__}: {exc}"})


def serve(host: str = "127.0.0.1", port: int = 8765) -> None:
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"memory-palace HTTP: http://{host}:{port}  （OpenAPI: /openapi.json）", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="memory-palace-http", description="本地 HTTP 记忆接口")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--allow-remote", action="store_true",
                        help="允许绑定非本机地址（危险：接口没有鉴权，会读到你全部记忆）")
    args = parser.parse_args(argv)
    if args.host not in ("127.0.0.1", "localhost", "::1") and not args.allow_remote:
        parser.error("非本机地址需要 --allow-remote，且请自行加鉴权/反向代理")
    serve(args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
