"""test_integrations.py - MCP 与 HTTP 两条通用接入层的契约自测。

    python tests/test_integrations.py          # 直接跑
    python -m pytest tests/test_integrations.py -q

测的是「别的 agent 真的能把我们当工具用」这件事，所以断言全在协议层：
握手、工具清单、工具调用、错误语义、OpenAPI 可导入的形状。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import urllib.request
from http.server import ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from memory_palace import http_api  # noqa: E402


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{label} {detail}".strip())
    print(f"  [PASS] {label}" + (f"  · {detail}" if detail else ""))


def test_mcp(store: str) -> None:
    env = dict(os.environ)
    env["MEMORY_PALACE_STORE"] = store
    env["PYTHONPATH"] = ROOT + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.Popen(
        [sys.executable, "-m", "memory_palace.mcp_server"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", env=env, cwd=ROOT,
    )

    def rpc(message: dict) -> dict:
        proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        proc.stdin.flush()
        return json.loads(proc.stdout.readline())

    try:
        init = rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18", "capabilities": {}}})
        check(
            "MCP 握手：回显协议版本与 serverInfo",
            init["result"]["protocolVersion"] == "2025-06-18"
            and init["result"]["serverInfo"]["name"] == "memory-palace",
            str(init["result"]["serverInfo"]),
        )

        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        proc.stdin.flush()

        tools = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
        names = [t["name"] for t in tools]
        check(
            "工具清单含 context/remember/recall/cycle，且每个都有 inputSchema",
            {"memory_context", "memory_remember", "memory_recall", "memory_cycle"} <= set(names)
            and all("inputSchema" in t for t in tools),
            f"{len(names)} 个工具",
        )
        check(
            "工具描述写的是「何时用」，不是参数复读（模型靠它决定要不要调）",
            all(len(t["description"]) > 20 for t in tools),
        )

        remember = rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
            "name": "memory_remember",
            "arguments": {"content": "禁止绝对化措辞，判断带概率", "tags": ["硬规则"],
                          "tier": "core", "pin": True}}})
        check(
            "memory_remember 返回记忆名（后续反馈要用）",
            remember["result"]["isError"] is False and "m:" in remember["result"]["content"][0]["text"],
            remember["result"]["content"][0]["text"],
        )

        ctx = rpc({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
            "name": "memory_context", "arguments": {"query": "周末去哪玩", "budget": 300}}})
        text = ctx["result"]["content"][0]["text"]
        check(
            "无关查询下仍注入钉死规则（MCP 侧与 CLI 侧行为一致）",
            "禁止绝对化" in text and "钉死" in text,
            text.splitlines()[0],
        )

        bad = rpc({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                   "params": {"name": "memory_nope", "arguments": {}}})
        check("未知工具返回 JSON-RPC -32601，而不是崩掉进程", bad.get("error", {}).get("code") == -32601)

        soft = rpc({"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {
            "name": "memory_outcome", "arguments": {"name": "m:missing", "result": "loss"}}})
        check(
            "业务失败用 isError=true 回给模型（让它自己纠正），不是协议错误",
            soft["result"]["isError"] is True and "没有这条记忆" in soft["result"]["content"][0]["text"],
        )
    finally:
        proc.stdin.close()
        proc.wait(timeout=15)
    check("stdin 关闭后进程正常退出", proc.returncode == 0, str(proc.returncode))


def test_http(store: str) -> None:
    os.environ["MEMORY_PALACE_STORE"] = store
    server = ThreadingHTTPServer(("127.0.0.1", 0), http_api.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def call(path: str, payload: dict | None = None) -> tuple[int, dict]:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(base + path, data=data,
                                     headers={"Content-Type": "application/json"},
                                     method="GET" if payload is None else "POST")
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8") or "{}")

    try:
        code, health = call("/health")
        check("HTTP /health 可用", code == 200 and health["ok"] is True, str(health))

        code, saved = call("/remember", {"content": "用户偏好表格化输出，不要长篇", "tags": ["偏好"], "tier": "fact"})
        check("HTTP /remember 返回名字与情绪来源", code == 200 and saved["name"].startswith("m:"), str(saved))

        code, ctx = call("/context", {"query": "输出格式", "budget": 300})
        check(
            "HTTP /context 返回可直接注入的 text 与用量",
            code == 200 and ctx["text"].startswith("# 记忆上下文") and ctx["used"] <= 300,
            f"used={ctx['used']}",
        )

        code, spec = call("/openapi.json")
        paths = set(spec.get("paths", {}))
        check(
            "OpenAPI 3.0 可供 Dify/Coze/n8n 导入，operationId 齐全",
            code == 200 and spec["openapi"].startswith("3.")
            and {"/context", "/remember", "/recall"} <= paths
            and all("operationId" in spec["paths"][p]["post"] for p in paths),
            ", ".join(sorted(paths)),
        )

        code, _ = call("/context", None)
        check("GET 不支持的路由返回 404 且带路径提示", code == 404)

        code, err = call("/outcome", {"name": "m:missing", "result": "loss"})
        check("未知记忆返回 404 而不是 500", code == 404 and "没有这条记忆" in err.get("error", ""))

        code, err = call("/remember", {"content": ""})
        check("空内容返回 400（客户端错误，不是服务端故障）", code == 400)
    finally:
        server.shutdown()
        server.server_close()


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="mp-int-") as tmp:
        print("MCP（stdio）")
        test_mcp(os.path.join(tmp, "mcp.json"))
        print("HTTP + OpenAPI")
        test_http(os.path.join(tmp, "http.json"))
    print("\n接入层契约自测通过 ✅")


def test_mcp_contract() -> None:
    with tempfile.TemporaryDirectory(prefix="mp-mcp-") as tmp:
        test_mcp(os.path.join(tmp, "mcp.json"))


def test_http_contract() -> None:
    with tempfile.TemporaryDirectory(prefix="mp-http-") as tmp:
        test_http(os.path.join(tmp, "http.json"))


if __name__ == "__main__":
    main()
