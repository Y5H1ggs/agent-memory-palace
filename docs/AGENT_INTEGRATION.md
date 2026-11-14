# 与 agent 结合：把记忆当成「上下文预算」来管

这份文档只回答一个问题：**一个 agent 应该在什么时机、用什么命令、拿什么去做什么。**

先说清楚它解决的是哪一类问题。agent 的记忆通常有三种做法，各有各的坑：

| 做法 | 坑 |
|---|---|
| 全塞进上下文 | 越聊越贵，最后被截断，早期约定悄悄消失 |
| 向量库 RAG | 只能找回「相似的文档」，找不回「你答应过我什么」「我上次错在哪」 |
| 每次手动写进 system prompt | 人能维护的规则不超过 10 条，且永远不清理 |

memory-palace 的位置在第二和第三种之间：**它管的不是文档，是「我该记住什么、放下什么」**。
所以它给 agent 的接口不是 `search(query) -> chunks`，而是 `context(task, budget) -> 一小块文本`。

## 一、三种接入时机（成本从低到高）

### 1. 会话开始：注入一块（必做）

```bash
memory-palace context --query "用户问的是仓位分配" --budget 800
```

输出是一段可直接拼进 system prompt 的文本，形如：

```
# 记忆上下文（候选 12 条 → 取 5 条，412/800 字，语义检索「仓位分配」）

- [硬规则] 禁止绝对化措辞，所有判断都带概率（钉死）
- [仓位] 半凯利：单笔不超过总资金三分之一
- [教训] 追高后逆板块阴跌，亏三个点，很后悔（细节可能失真 0.67）

## 自述（第 4 版）
起源「输出」2条 → 转折「复盘」3条 → 当下「仓位」2条｜可靠度 0.91｜单主题占比 0.32

## 提示
- 1 条记忆情绪强、可靠度 < 0.70：引用其细节时标注不确定
- 生效输出风格：输出尽量精简
```

三个设计要点，都是踩过坑才这么定的：

1. **钉死的记忆永远在**，哪怕和当前问题无关。只在语义命中时才注入的「规则」，其实只是资料。
2. **带可信度**。情绪强的记忆更难忘也更可能是错的，注入时告诉模型「这条细节存疑」，
   比假装它同样可靠要诚实。
3. **预算硬约束**。`--budget` 是字符数；钉死条目超预算时仍然保留，并在提示里说明超了多少。

### 2. 会话结束：固化结论（建议做）

```bash
memory-palace remember "用户要求所有输出表格化，不要长篇" --tags 偏好 --tier fact
memory-palace remember "追高汉缆，逆板块阴跌 -2.9%，教训：先看板块资金接力" --tags 复盘,教训
memory-palace cycle
```

`cycle` 做四件事：把反复出现的碎片**沉淀**成结论、按价值**遗忘**、重新**自述**、
**反思**并（在边界内）自修正。**不跑 cycle 也不会坏**，只是记忆会一直是一堆碎片。

### 3. 交互中：反馈与纠错（最容易被忽略，但最值钱）

```bash
memory-palace outcome m:719af3d5 contradicted   # 用户纠正了你 → 那条记忆变"存疑"
memory-palace outcome m:c9d1edd5 confirmed      # 用户认可 → 轻微正向
memory-palace style observe concise "输出尽量精简" --sign +1
memory-palace style observe verbose "输出充分展开" --sign +1 --opposes concise
```

`outcome` 只改情绪档案，**不改记忆强度**（回想一次和感受一次是两件事）。
`style` 是风格固化：同一偏好被观察到 3 次以上、跨过观察期，才会真正生效；
对立的两条不会同时生效，证据不足时两条一起停用并上报。

## 二、一个完整的 agent 循环

```python
# 伪代码：任何 agent 框架都能照搬
ctx = sh("memory-palace context --query {task} --budget 800")
system_prompt = BASE_PROMPT + "\n\n" + ctx

answer = llm(system_prompt, user_input)

for fact in extract_durable_facts(answer, user_input):
    sh(f'memory-palace remember "{fact}" --tags {tags}')

if user_corrected_us:
    sh(f"memory-palace outcome {blamed_memory} contradicted")
if session_end:
    sh("memory-palace cycle")
```

## 三、什么时候**不要**用它

| 场景 | 该用什么 |
|---|---|
| 「这份 200 页 PDF 第 7 章讲了什么」 | RAG / 文档检索 |
| 「上周三的会议纪要原文」 | 日记/文件系统，不是记忆 |
| 需要精确事实、不能出错 | 数据库 / 检索工具，记忆只能给线索 |
| 多个进程同时写同一份记忆 | **0.10 起支持**：SQLite WAL + `BEGIN IMMEDIATE` + 变更前重载；写热点会排队（单写者设计）。详见 `docs/STORAGE.md` |
| 需要跨机器同步 | 没有后端，只有一个 JSON 文件 |

一句话：**它记的是「判断与教训」，不是「原文」。**

## 四、多套记忆怎么分

用 `--store` 或环境变量 `MEMORY_PALACE_STORE`：

```bash
# 跨项目通用的用户偏好与硬规则
MEMORY_PALACE_STORE=~/.memory-palace/user.json memory-palace context

# 项目 A 的教训
MEMORY_PALACE_STORE=~/proj-a/.memory.json memory-palace remember "..." --tags 复盘
```

建议两类：**user 级**（偏好、硬规则、沟通风格）与**project 级**（该项目的结论与教训）。
不要把项目细节写进 user 级，否则换个项目就会被无关记忆污染。

## 五、成本

| 操作 | 耗时（本机，4~200 条记忆） |
|---|---|
| `remember` | ~60ms（Python 启动占大头） |
| `context` | ~80ms（首次查询要重建语义索引） |
| `cycle` | ~120ms |

所以：**每个会话注入一次、每轮结束固化一次**是合理频率；
不要在每个 token 上调用它，也不要在 10 轮对话里调 50 次 `cycle`。

## 六、为什么不让模型直接调库

也能，但 CLI 有三个实际好处：

1. **独立进程**：崩了不会带走 agent 主流程；
2. **可审计**：每条命令都能在日志里看到输入输出，出问题能复现；
3. **可替换**：以后换成 SQLite 后端或远程服务，agent 侧一行都不用改。
