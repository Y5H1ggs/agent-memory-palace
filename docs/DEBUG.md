# 调试：四层定位法

> 同一句「它记不住」，可能卡在四个完全不同的地方，修法互不相干。
> 这份文档不教安装（那是 [PORTABLE.md](PORTABLE.md)），只教**怎么判断卡在哪一层**。

先跑一遍：

```bash
python verify.py            # 分五层报告，每层给一条判据
python verify.py --full     # 加跑 demo 的 20 条断言
```

它不过的地方就是起点。下面按层展开。

## 第 0 层：代码能不能导入

```bash
python -c "import memory_palace; print(memory_palace.__version__)"
```

| 症状 | 原因 | 修 |
|---|---|---|
| `ModuleNotFoundError: memory_palace` | 不在仓库根，或没 `pip install -e .` | `cd` 到仓库根再跑；或 `export PYTHONPATH=<仓库根>` |
| `SyntaxError` / 奇怪的缩进错误 | Python < 3.10 | `python -V`，换 3.10+ |
| 导入很慢（>10s） | 自动探测在等嵌入服务超时 | 正常上限 0.35s；超过说明 `embedding_endpoint` 指向了一个黑洞地址 |

**这一层不过，别往下看。** 上面的错误会伪装成各种下游问题。

## 第 1 层：库能不能打开

```bash
memory-palace fsck
memory-palace status
```

| 症状 | 原因 | 修 |
|---|---|---|
| `快照缺少配置项: [...]` | 0.13.0 的旧 bug | 升到 0.13.1+ |
| `attempt to write a readonly database` | 库在网络盘 / 多进程抢 WAL | 挪到本地盘；0.13 起有 3 次重连 |
| `校验和不符` / `StoreCorrupt` | 拷贝被截断 | 用 `export` / `import` 重搬，别直接拷 |
| `no such table: nodes` | 拿了一个空文件当库 | 删掉重建，或从 `data/` 导入 |

**判据**：`fsck` 的 `integrity` 必须是 `ok`，`orphan_edges` 必须是 0。

## 第 2 层：检索有没有结果

```bash
memory-palace recall "你确定库里有的一个词" -k 3
memory-palace explain "同一个词"
```

| 症状 | 真正的原因 | 怎么确认 |
|---|---|---|
| 有内容但 `recall` 空 | **作用域不匹配**：写进 `proj:A`，却从 `proj:B` 查 | `memory-palace projects` 看分布在哪个作用域；加 `--scope all` 再试 |
| 有结果但排序很怪 | 阈值被改过 / 换了嵌入模型没重扫 | `memory-palace calibrate` |
| 只有 1-2 条命中，其余全丢 | 相对门槛把「字面沾边」挡掉了 | 这是**设计**，不是 bug；看 `explain` 里每条的 semantic 分量 |
| 稠密通道命中率反而下降 | 模型与语言不匹配（英文模型跑中文） | `memory-palace embed --check`，差距 < 0.2 就是不可用 |

**判据**：`explain` 能逐条列出「语义 / 标签 / 时近」三个分量。列不出来说明走的是老路径。

## 第 3 层：注入是不是空的

```bash
memory-palace context --query "你的问题" --budget 400
memory-palace context --status-only --query "你的问题"
```

| 输出 | 含义 | 下一步 |
|---|---|---|
| `（没有相关记忆…）` | **弃答**。这是正确行为，不是故障 | 换一个库里真有的说法；或先 `remember` |
| `取 0 条，0/0 字` | `--budget 0`，或调用方传了 0 | 给个正数 |
| 只有标题，没有条目 | 候选为空（作用域不对 / 库是空的） | 回到第 1、2 层 |
| 有全文但用户说「没看到」 | **宿主没把输出放进 prompt** | 见第 4 层 |

**判据**：`--status-only` 的数字必须与 `--budget` 相符。`240/900` 是正常，`240/0` 是配置错了。

## 第 4 层：宿主有没有真的用上

这一层最容易误判：**记忆注入了，但模型没遵守**，看起来像「记忆没生效」。

| 宿主 | 判据 | 常见坑 |
|---|---|---|
| **WorkBuddy skill** | 说「上次说过什么」，它是否会跑 `context` | skill 目录的 `.home` 必须写仓库绝对路径；调用时 cwd 要在目标项目里 |
| **MCP** | `memory_context` 工具是否出现在工具列表 | `mcp.json` 里 `PYTHONPATH` 必须指向仓库根；改完要重启宿主 |
| **DSH 插件** | 终端是否打印 `[memory-palace] plugin loaded` | `cordis.yml` 里的 `__REPO__` / `__HOME__` 没替换 |
| **CLI 手调** | 上面三层都过，就说明库没问题 | 那是宿主侧的事，别再改库 |

**铁律**：同一个宿主只开一种接入（skill **或** MCP **或** 插件）。
两种同时开，会得到「今天记得、明天忘了」——其实是两套调用在写同一个库的入口。

## 一张速查表

| 你看到的 | 先查这一层 | 一条命令 |
|---|---|---|
| 导入报错 | 0 | `python -c "import memory_palace"` |
| 库打不开 / 校验失败 | 1 | `memory-palace fsck` |
| 明明写过却查不到 | 2 | `memory-palace projects` |
| 查到了但注入是空 | 3 | `memory-palace context --status-only --query x` |
| 注入有内容但模型不遵守 | 4 | 看宿主的工具列表 / 插件日志 |
| 越用越慢 | 2 | `python -m memory_palace.bench 10000` |

## 别做的四件事

1. **别手改 sqlite**。改完墓碑、语义索引、逻辑边三者不一致，症状会在三天后出现。
2. **别在库打不开时先接宿主**。那时你分不清是库坏了还是接错了，会把两个问题搅成一个。
3. **别为了提高命中率把 `semantic_min_score` 调到 0**。那会让弃答失效，模型开始拿无关记忆编答案。
4. **别同时开两套接入**。见上。
