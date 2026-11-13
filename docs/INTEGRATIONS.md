# 与其他 agent 接入

不是「某某平台专用」。这套东西对外只有三个面，任何宿主都能从其中一个接进来：

| 接入面 | 谁用 | 稳定性 |
|---|---|---|
| **MCP**（stdio） | Claude Code、Codex CLI、Cursor、Cline、Windsurf、Zed、Gemini CLI、Continue… | 协议稳定，配置字段随宿主版本微调 |
| **HTTP + OpenAPI** | Dify、Coze、n8n、自定义 GPT Actions、低代码/工作流平台 | 最通用，但要自己起进程 |
| **CLI / Python 库** | 自己写循环的 agent、脚本、定时任务、批处理 | 最可控，无协议依赖 |

一条原则：**记忆的语义在哪一层都一样**——`context` 注入、`remember` 固化、`cycle` 代谢。
换宿主只换管道，不换用法。

---

## 一、MCP（推荐，一次配置处处可用）

先装好（任选其一）：

```bash
pip install -e /path/to/memory-palace      # 之后命令是 memory-palace-mcp
# 或者不装，直接用模块：
python -m memory_palace.mcp_server
```

通用启动参数永远是这三样：`command` / `args` / `env`。唯一的硬要求是
**`MEMORY_PALACE_STORE` 指向一个可写路径**（默认 `~/.memory-palace/store.json`）。

> 注意：MCP 的 stdio 传输是**换行分隔的 JSON-Messages**，不是 LSP 的 `Content-Length` 头。
> 服务端也不会往 stdout 打任何日志（日志一律走 stderr），否则会让客户端解析失败。

### Claude Code

项目根建 `.mcp.json`（团队共享），或 `~/.claude.json` 里加同名段：

```json
{
  "mcpServers": {
    "memory-palace": {
      "command": "memory-palace-mcp",
      "env": { "MEMORY_PALACE_STORE": "/Users/you/.memory-palace/store.json" }
    }
  }
}
```

命令行等价（更省事）：

```bash
claude mcp add memory-palace --scope user -- memory-palace-mcp
```

### Codex CLI

`~/.codex/config.toml`：

```toml
[mcp_servers.memory-palace]
command = "memory-palace-mcp"
env = { MEMORY_PALACE_STORE = "/Users/you/.memory-palace/store.json" }
```

Codex 还会读仓库根目录的 `AGENTS.md` —— 本仓库自带一份，写清了「什么时候该调记忆」，
所以即使不配 MCP，它也能照那个约定用 CLI。

### Cursor

`~/.cursor/mcp.json`（全局）或项目 `.cursor/mcp.json`：

```json
{ "mcpServers": { "memory-palace": { "command": "memory-palace-mcp" } } }
```

### DSH（DeepSeek Harness）

DSH 不直接读 `mcp.json`，而是通过官方桥接插件 `@deepseek-ai/dsh-mcp-client`，
配置写在 **每个 profile 一份** 的 `cordis.patch.yml` 里，保存后约 1 秒热重载（`patchReload: live`）。

```yaml
# ~/.dsh/profiles/<profile>/cordis.patch.yml
- insert:
    - id: mcp-memory-palace
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: memory-palace
        transport: stdio
        command: python
        args: ['-u', '-m', 'memory_palace.mcp_server', '--profile', 'slim']
        cwd: /path/to/memory-palace
        env:
          MEMORY_PALACE_STORE: /Users/you/.memory-palace/store.json
          PYTHONPATH: /path/to/memory-palace
        toolCallTimeoutMs: 30000
```

不想手算出绝对路径，直接打印：

```bash
memory-palace config --for dsh            # slim 档（推荐）
memory-palace config --for dsh --profile full
memory-palace config --for claude|codex|cursor|http
```

三条 DSH 特有的坑，都踩过：

1. **工具名会变成 `mcp__memory-palace__memory_context`**。`serverName` 里的连字符会保留，
   会话历史与权限规则按这个名字记 —— 改名会断开旧记录，起名后别改。
2. **子进程的环境变量会被清洗**：名字里含 `KEY` / `PASSWORD` / `SECRET` / `TOKEN` / `DSH_*` 的都会被剥掉，
   再合并你显式写的 `env`。所以 `MEMORY_PALACE_STORE` 必须显式写进 `env`，别指望从 shell 继承。
3. **工具定义全量常驻上下文**（DSH 当前不支持 Tool Search）。8 个工具约 1.2k token，每轮都付。
   用 `--profile slim` 只暴露 `context / remember / cycle` 三个，省掉约 60% 的工具 schema。
   想看清单：`python -m memory_palace.mcp_server --list-tools`。

### Cline / Windsurf / Gemini CLI / Zed

都是同一套 `command + args + env`，只是 JSON 位置不同：

| 宿主 | 文件 | 顶层键 |
|---|---|---|
| Cline（VS Code 扩展） | 扩展设置里的 MCP Servers → Configure | `mcpServers` |
| Windsurf | `~/.codeium/windsurf/mcp_config.json` | `mcpServers` |
| Gemini CLI | `~/.gemini/settings.json` | `mcpServers` |
| Zed | `~/.config/zed/settings.json` | `context_servers`（字段是 `command: {path, args}`） |

字段名与路径以各宿主官方文档为准；`command/args/env` 的语义是 MCP 协议层的，不随宿主变。

---

## 二、HTTP + OpenAPI（给工作流平台）

```bash
memory-palace-http --port 8765        # 默认只绑 127.0.0.1，无鉴权
```

| 平台 | 怎么接 |
|---|---|
| **Dify** | 工具 → 自定义 → 导入 OpenAPI：`http://127.0.0.1:8765/openapi.json` |
| **Coze / 扣子** | 插件 → 创建插件 → 导入 OpenAPI（同上地址；若平台要求公网，套一层 HTTPS 反代） |
| **n8n** | HTTP Request 节点，`POST /context`、`POST /remember` |
| **自定义 GPT** | Actions → 导入 OpenAPI；需要公网 HTTPS 地址 |
| **任何脚本** | `curl -X POST localhost:8765/context -d '{"query":"…"}'` |

端点：`/context`、`/remember`、`/recall`、`/outcome`、`/status`、`/cycle`、`/health`、`/openapi.json`。
错误语义按 HTTP 来：400 参数错、404 对象不存在、500 内部错误。

> 这是一个能读你**全部记忆**的接口。默认不绑公网；要跨机访问请自己加 token + 反代，
> 并且想清楚：你愿意让谁看到你的教训。

---

## 三、代码里直接用（LangChain / OpenAI / 自己的循环）

MCP 和 HTTP 都是管道，真正的逻辑在库里。自己写循环时直接调用最省事：

```python
from memory_palace import MemoryPalace

palace = MemoryPalace(path="~/.memory-palace/store.json")

block = palace.context_for("仓位怎么分配", budget=800)   # 见下方说明
palace.remember("追高后逆板块阴跌 -2.9%，教训：先看资金接力", tags=("复盘", "教训"))
palace.feel_outcome("m:1a2b3c4d", "contradicted")
palace.run_cycle()
```

> 库本身不提供 `context_for`；等价的写法是 `from memory_palace.cli import build_context, format_context`，
> 或直接调 MCP/HTTP。**不想引入协议就别引**：三行代码也能拼出注入块。

LangChain / LangGraph（把 CLI 当工具，最不容易踩版本坑）：

```python
import subprocess, json
from langchain_core.tools import tool

@tool
def memory_context(query: str, budget: int = 800) -> str:
    """取一段受预算约束的长期记忆，用于本轮 system prompt。"""
    out = subprocess.run(["memory-palace", "context", "--query", query, "--budget", str(budget)],
                         capture_output=True, text=True, encoding="utf-8")
    return out.stdout or out.stderr
```

OpenAI function calling：把 `memory_palace.mcp_server.TOOLS` 里的 `inputSchema` 直接当 `parameters`
（字段名 `inputSchema` → `parameters`），handler 里调 `HANDLERS[name](arguments)` 即可 —— 不必重写一遍 schema。

---

## 四、没有框架：把约定写进提示词

最土也最稳的一种。在 system prompt 里加五行：

```
你有长期记忆，用 `memory-palace` 命令访问（存储 ~/.memory-palace/store.json）。
- 任务开始前：memory-palace context --query "<任务>" --budget 800，把输出原样放在最前面。
- 用户说「记住」时：memory-palace remember "<一句话结论>" --tags … --tier fact（硬规则加 --pin）。
- 用户纠正你时：memory-palace outcome <名字> contradicted。
- 长任务收尾：memory-palace cycle。
- 不要每轮都跑 cycle，也不要手改 JSON。
```

仓库根目录的 `AGENTS.md` 就是这份约定的完整版，Codex / Claude Code 等会自动读。

---

## 五、不管接哪条管道，时序都一样

```
任务开始   context(query=任务, budget≈800)   → 拼进 system prompt
交互中途   outcome(名字, contradicted)        → 用户纠正时
任务结束   remember(结论) × N                 → 只记结论，不记过程
收尾       cycle()                            → 沉淀/遗忘/自述/反思
```

四条经验，都是实测踩出来的：

1. **`context` 每次会话调一次就够**。它已经按预算和重要性排过序，多调只会重复。
2. **`--pin` 只给真正的硬规则**。钉死的记忆永不衰减、永不自动遗忘，滥用等于把系统变成便签本。
3. **`cycle` 是代谢不是日志**。每轮都跑会让碎片来不及沉淀 —— 一轮长任务一次。
4. **多条会话共享一个 store 时，不要并发写**。写入是原子的，但锁不跨进程。

---

## 六、常见问题

| 现象 | 原因 / 处理 |
|---|---|
| MCP 客户端显示服务已连上但工具列表为空 | `tools/list` 需要在 `initialize` 之后；检查是否有别的进程往 stdout 打过日志 |
| 工具调用报 `找不到 memory_palace 库` | 用绝对路径的 Python，或设 `PYTHONPATH` 指向仓库根 |
| Windows 上中文输出乱码/报编码错 | 0.2.0 起 CLI 会自动切 UTF-8；宿主若自建管道，设 `PYTHONUTF8=1` |
| 多台机器想同步记忆 | 现在只有一个 JSON 文件，没有后端；自行用同步盘，**但别同时写** |
| 想让两个项目共用记忆 | 别共用。用两个 store，分别在任务开始时选一个 |
