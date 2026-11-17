# 搬到另一台机器

> 目标：在**没有网络也能跑**的前提下，把这套东西和数据一起搬过去。
> 全程只需要 Python ≥ 3.10，不需要 `pip install` 任何第三方包。

## 0. 最短路径（目标机已经装了 ollama）

```bash
unzip agent-memory-palace-*.zip && cd agent-memory-palace
python -m pip install -e .          # 可选；不装也能用 python -m memory_palace.cli

ollama pull bge-m3                  # 一次，约 1.2 GB
ollama serve                        # 如果还没在跑

memory-palace embed --check         # 同义句余弦必须明显高于无关句
memory-palace demo                  # 六幕，20 条断言，约 2 秒
```

`embedding_provider` 默认是 `auto`，它会自己探测本机 `11434` 端口 ——
**不需要改配置**，ollama 起来了就是语义通道。

## 1. 搬记忆（不是搬代码）

代码是仓库，记忆是库文件。两者分开搬：

### 推荐：用 export / import（带校验和，跨版本安全）

```bash
# 旧机器（位置参数，不是 --out）
memory-palace export memory-2026-11-17.zip

# 新机器（导入前会自动备份现有库）
memory-palace import memory-2026-11-17.zip
```

### 直接拷文件（同版本时最省事）

```bash
# 位置：~/.memory-palace/store.sqlite（Windows: C:\Users\<你>\.memory-palace\store.sqlite）
# 连同 -wal / -shm 一起拷；或者先让所有进程退出，只拷主文件
```

三条注意：

1. **先退出所有写入进程**（MCP 服务、长驻 CLI），否则 `-wal` 里还有没合并的页。
2. **不要放网络盘**（NFS / SMB / 群晖共享）。WAL 依赖本机 POSIX 锁语义，网络盘上会偶发
   `attempt to write a readonly database`，而且不报原因。
3. 配置存在库的快照里 —— **搬库 = 搬配置**。不想带走旧配置就在新机器上 `--set` 覆盖。

## 2. 嵌入：三条路，选一条

| 方案 | 需要什么 | 适合 |
|---|---|---|
| **本机 ollama**（推荐） | `ollama serve` + 一个模型 | 离线、隐私、零依赖 |
| 本机 sentence-transformers | `pip install sentence-transformers`（会拉 torch，约 2 GB） | 已经装了 torch 的机器 |
| 任意 OpenAI 兼容端点 | 一个能回 `/v1/embeddings` 的服务 | 局域网有 GPU 服务器 |

```bash
# ollama（默认端口）
ollama pull bge-m3
memory-palace config --set embedding_model=bge-m3
memory-palace config --set embedding_provider=ollama

# ollama 在别的机器 / 别的端口：先填地址，最后再切 provider
memory-palace config --set embedding_endpoint=http://192.168.1.20:11434
memory-palace config --set embedding_provider=ollama

# OpenAI 兼容端点（vLLM / TEI / LM Studio / 云厂商）
memory-palace config --set embedding_endpoint=http://127.0.0.1:8000/v1/embeddings
memory-palace config --set embedding_model=bge-m3
memory-palace config --set embedding_provider=http     # ← 必须最后
echo $MEMORY_PALACE_EMBED_KEY        # 需要鉴权时用这个环境变量，别写进配置文件

memory-palace embed --check          # 必须过：同义 vs 无关的余弦差距 ≥ 0.2
memory-palace calibrate              # 换模型后重扫阈值
```

**为什么换模型要 `calibrate`**：不同模型的余弦分布不一样。旧阈值在新模型上会变成
「全是命中」或「全被弃答」，而这两种都表现为「检索坏了」，很难往回追。

## 3. 能不能用 DeepSeek 的 API？

分两件事，别混：

| 用途 | 能不能 | 原因 |
|---|---|---|
| **嵌入（embedding）** | ❌ **不能** | DeepSeek 官方 API 没有 embeddings 端点（截至 2026-10 只有 chat / responses / FIM / models / balance / files）。定价页也只有对话模型 |
| **反思（LLM 复盘）** | ✅ 可以 | 它是标准 OpenAI 兼容 `/chat/completions` |

所以「用 DS 的 API 做语义检索」这条路不通。想要远程嵌入，用别家的 embedding 服务，
或者在那台有 ollama 的机器上本地跑（更便宜、更私密）。

DS 用作**反思**（`reflect`，让模型读指标提假设）这样配：

```bash
memory-palace config --set llm_endpoint=https://api.deepseek.com/v1/chat/completions
memory-palace config --set llm_model=deepseek-flash
memory-palace config --set llm_provider=http          # ← 必须最后
export MEMORY_PALACE_LLM_KEY=sk-...        # 只放环境变量

memory-palace reflect                      # 应输出 findings / hypotheses
```

注意它的边界：**模型只能提建议，不能改配置**。白名单外的参数会被拒、原话留档进
`insights` 待人工裁决；第二轮会把拒绝原因回灌给它再问一次。

## 4. 装不装 skill

`memory-palace-skill-*.zip` 是给 WorkBuddy / Claude Code 这类宿主的**适配层**，与库解耦：

```bash
# WorkBuddy：解压到 ~/.workbuddy/skills/memory-palace/
# 里面的 .home 要写新机器的仓库绝对路径（一行）
echo "D:/path/to/agent-memory-palace" > ~/.workbuddy/skills/memory-palace/.home
```

`.home` 写错的表现是 `找不到 memory_palace 库` —— 它只认这一行，不猜。

## 5. 新机器上先跑这三条

```bash
python -m memory_palace.demo          # 20 条断言，验证代码本身没问题
memory-palace fsck                    # 验证搬过来的库没坏
memory-palace context --query "测试" --budget 400   # 验证检索与注入能跑
```

三条都过，再谈接入 MCP / HTTP。**别在数据存疑的时候先接宿主** ——
那时你分不清是库坏了还是接错了。

## 6. 常见坑（都是真踩过的）

| 现象 | 原因 | 处理 |
|---|---|---|
| `快照缺少配置项: [...]` | 0.13.0 的 bug，已修 | 升到 0.13.1+ |
| `attempt to write a readonly database` | 多进程首次建库抢 WAL / 库在网络盘 | 0.13 起自动重连 3 次；仍报错就把库挪到本地盘 |
| 中文变成乱码 | Windows 控制台默认 GBK | `set PYTHONUTF8=1`（或 PowerShell `$env:PYTHONUTF8=1`） |
| `找不到 memory_palace 库` | skill 的 `.home` 路径不对 | 写绝对路径，一行，不要引号 |
| 换模型后检索全乱 | 阈值没重扫 | `memory-palace calibrate` |
| `llm_provider=http 时必须提供 llm_endpoint` | `--set` 顺序反了：先切 provider、后填地址 | **先填 endpoint/model，最后切 provider** |
| 路径里有空格 | 没加引号 | `--store "D:/My Data/store.sqlite"` |

## 7. 停机搬迁（不想用 export）

```bash
memory-palace fsck                       # 1. 确认库是好的
# 2. 退出所有长驻进程（MCP server / HTTP 服务 / 宿主里的 agent）
cp ~/.memory-palace/store.sqlite*  /目标/  # 3. 连 -wal/-shm 一起拷
memory-palace fsck                       # 4. 新机器上再体检一次
```

第 4 步不能省：拷贝过程中的截断不会报错，只会在某次读取时表现为「少了几条记忆」。
