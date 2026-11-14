# Changelog

所有值得写下来的变更都在这里。格式参照 [Keep a Changelog](https://keepachangelog.com/)，版本号遵循 [SemVer](https://semver.org/lang/zh-CN/)。

## 为什么第一个公开版本是 0.1.0 而不是 1.0.0

SemVer 里 `1.0.0` 的含义是「公开 API 已稳定，可以放心依赖」。这个项目现在还不满足：

- 没有任何外部使用者，API 是按内部需要长的（`run_cycle` 的返回结构、`MemoryPalace` 的构造参数都可能变）；
- 语义层是概念种子 + 字符 bigram + 语料学习，没有独立评测集，召回上限未知；
- 只有单写者假设，没有跨进程文件锁。

**升到 1.0.0 的条件**（四条全部满足）：外部使用者 ≥ 1、CI 覆盖 Linux/Windows/macOS、语义层有公开评测集与基线数字、跨进程写锁落地。

---

## [0.9.1] - 2026-11-14

语用层（语言是记忆的一部分）+ 绿灯审计工具。按计划留在 0.9 线上，1.0.0 仍是正式版目标。

### Added

- **`pragmatics.py`（语用层）**：一句话是什么行为、有多硬、为什么说。
  `Speech(act, force, register, motive, hedged, confidence, evidence)`。
  - 行为：禁令 / 要求 / 请求 / 偏好 / 暗示·不满 / 纠正 / 撤回 / 确认 / 自述 / 提问 / 陈述
  - 强度：`基础 × 模糊限定(0.6) × 强调`，并据此建议层级（两级衰减）
  - 场景：review / execution / coding / chat，**看不出就老实返回 chat**
  - 全部规则法 + evidence，可指着原话解释，不引入模型
- **`tools/greenlight_audit.py`**：变异测试体检。12 个变异（衰减、弃答、IDF、否定词、
  风格裁决、作用域归一、双时态、去重、撤回语义、模糊降级、跨项目沉淀、语用合并），
  每个都指定「该由哪些测试发现」。**当前 12/12 被抓，0 漏洞。**
- **`tests/test_pragmatics.py`**（26 项）+ **`docs/PRAGMATICS.md`**。

### Changed

- `remember(..., auto_tier=False)`：默认只**标注**语用档案，不动调用方给的层级；
  `auto_tier=True`（CLI `--auto-tier` / MCP 默认开）才按建议分层。
  刻意不做「自动 pin」——钉死是用户主权。
- `context` 注入块按语用打标：`（禁令）` / `（不满·未确认）` / `（撤回（不等于同意））`。
- `MemoryNode.speech` 参与序列化；合并时**更硬的那次覆盖更软的**。

### 核心语义（这一版真正想守住的）

| 说法 | act | force | tier | is_preference |
|---|---|---|---|---|
| 以后都别用表格了 | prohibition | 0.90 | core | ✅ |
| 别用表格 | prohibition | 0.72 | fact | ✅ |
| 能不能别用表格 | request | 0.55 | event | ✅ |
| 表格是不是有点多 | complaint | 0.35 | event | ❌ |
| 我上次说过别用表格 | correction | 0.85 | event | ❌ |
| 随便你 | withdrawal | 0.25 | event | ❌ |

**「随便你」不是同意。** 把它记成偏好，等于拿到一张用户从未发出的许可。
`withdrawal / question / hint / complaint / correction` 一律不算稳定偏好。

### Fixed（由绿灯审计抓到）

- **假绿断言**：「合并时更硬的那次覆盖更软的」原来用
  「用表格呈现」vs「以后都别用表格了」——两句余弦与长度比都不够，
  **实际根本没走合并路径**，是新建了一条，断言却绿。改成两句文本完全相同、
  只换 speech 对象后，变异 `speech-force-kept` 立刻被抓。
- 测试里还留着一段**安慰剂代码**（`p.classify_enabled = False`，该属性根本不存在），
  已删 —— 它不产生任何效果，只是让测试看起来更严格。

### 版本策略

**暂不推 1.0.0。** 0.9.x 继续消化小版本；1.0.0 的四条硬条件（外部使用者、语义评测集、
跨进程并发、API 冻结）一条都没达成，标 1.0 就是另一种绿灯幻觉。

自测：215 + 30 + 17 + 24 + 24 + 14 + 16 + 26 = **366**，外加 12 个变异体检全抓。

---

## [0.9.0] - 2026-11-14

「词典是基础」——这一版只做词法层，因为上一版那个「输出」误聚的根在这里。

### Added

- **`lexicon.py`（词法层）**，四件事，每件都修一个具体的错：
  1. **归一**：NFKC + 小写。`ＵＴＦ－８` / `utf-8` / `UTF8` 归成同一个词。
  2. **分词**：中文双字 bigram + 标识符（`env.sh` → `env.sh`/`env`/`sh`）+ 单字过滤，
     但**否定词保留**。
  3. **否定词**：`不没无非别勿禁未莫` 单字保留并 ×2 权重。
     「不追高」与「追高」余弦 **0.359**（自身重复 1.000）—— 漏掉它会把结论说反。
  4. **IDF**：`log((N+1)/(df+1))+1`，从语料现算、不落盘。
- **`tests/test_lexicon.py`**（16 项），含一条**反事实**：
  把 IDF 换成全 1，「构建机输出乱码」与「输出用表格」的余弦从 0.448 回到 **0.783**，
  两者会被聚成一类 —— 证明 IDF 真的在拦这件事。
- **`docs/LEXICON.md`**：四个决定、性能账、四条边界。

### Changed

- 字符维 **64 → 256**。64 个桶装几千个词项，碰撞本身就是误聚的第二个来源。
- 字符维从「每对字符等权 +1」改为「词项 × TF 饱和 × IDF」。
- `SemanticIndex` 新增 `encode_query()`：**任何与库内向量比余弦的地方都必须走它**
  （直接用 `encoder.encode()` 得到的是无 IDF 向量，与库内不在同一空间）。
- `recall` 的门槛现在要求「有词法证据」（语义或标签至少一路有贡献）。
  纯 recency 不再算相关：刚写进来的东西不该因为「新鲜」被召回。

### Fixed

- **去重静默失效**：`_find_duplicate` 用无 IDF 向量去比带 IDF 向量，余弦无意义。
  已改成同尺编码（`tests/test_temporal.py` 的去重断言恢复了）。
- **空 IDF 退化成概念单向量**：语料还没建索引时，所有词项权重为 0，向量只剩概念维，
  两条共享概念的记忆余弦会变成 **1.0**。改为「空 IDF 视为无 IDF」。
- **突变审计自己失效**：第 10 幕 patch 的是 `ConceptEncoder.encode`，
  而检索已改走 `encode_with` —— 坏编码器根本没生效。审计报 FAIL 不是它错，
  是它抓到了 patch 点过时。已同时 patch 两条路径。

### 性能（如实记账）

| 指标 | 前 | 后 |
|---|---|---|
| 200 条 `cycle` | 19 ms | 51 ms |
| 1000 条 `cycle` | 202 ms | 561 ms |
| 查询（热） | 1.5 ms | 3.1 ms |

三个缓存把损失从 12 倍压到 2.6 倍：分词结果 LRU（一次 rebuild 里同一文本要算两遍）、
聚类预取向量（原来 N² 对触发 2N² 次 lookup）、维度 64→256（这是正确性修复，不是优化）。

自测：215 + 30 + 17 + 24 + 24 + 14 + 16 = **340**。

---

## [0.8.0] - 2026-11-14

收到一份技术批评，说这库有三处"架构硬伤"。逐条跑代码核查后：
**一条论据被截断、一条结论对但归因错、一条不成立**；但它确实点出了我该补的东西 ——
不是代码，是**可发现性**。所以这一版不加机制，只把已经存在的能力变成契约。

### Added

- **`reflector=` 钩子**：外部反思（可接一次 LLM）。`ReflectionEngine` 默认仍是规则化监控，
  传 `reflector(payload) -> list[str]` 后，观察会以 `〔外部反思〕` 追加进 findings。
  三条护栏：不改配置（拿不到 suggestions）、只追加文字、抛异常只记 `reflector_error`
  不炸穿代谢。payload 含 metrics + 最近 40 条事件 + 自述摘要。
- **`examples/custom_encoder.py`**：默认编码器 vs 注入编码器的真实对比输出。
- **`tests/test_extensions.py`**（14 项）：把两个扩展点的行为变成契约，
  含"坏编码器抛异常时写入必须显式失败""维度不符被拒绝""外部反思不能改配置"。
- **`docs/CRITIQUE_RESPONSE.md`**：三条批评的逐条核查，附实测数据与完整代码引用。

### Changed

- `encoder=` 从"藏在构造函数里"变成**公开扩展点**：README 有新一节，示例 + 测试 + 文档齐备。
  （它从 v0.1 就在，只是没人知道 —— 能力存在但没人知道，等于不存在。）
- `ReflectionEngine` docstring 直说"这是规则化监控，不是 LLM 反思"。

### 核查结论（写进文档，也写在这里）

| 批评 | 结论 |
|---|---|
| 语义聚类只按标签，"完全丧失语义能力" | **论据被截断**：截图止于第 6 行，完整函数第 84–88 行有语义通道，`max(structural, 0.85*cosine)`；实测 0.000→0.773 |
| 同上，"抓不到字面不同但语义相近" | **结论成立，归因错**：不是没有通道，是默认编码器是 bigram（0.057）。已提供 `encoder=` 与示例 |
| 反思引擎是硬编码阈值告警 | **完全成立**，是命名问题：本来就是规则化监控。已加 `reflector=` 钩子 |
| 风格冲突只检出不裁决 | **不成立**：`active_statements` 只返回 `is_effective` 的条目，`suppressed_by` 会挡掉输家，第 3 幕有断言守着 |
| 换 SQLite + WAL 解决并发 | **诊断对、方案不对**：SQLite 解决语句原子性，不解决 `load→改→save` 的整体覆盖；且会丢掉"快照是可读 JSON"这个可调试性。正确解是乐观并发（state 带 revision，save 前比对）。**未实现**，已列待办 |

### 顺带发现的更严重问题（批评没提到）

默认编码器会**误聚**：`"构建机输出乱码"` 与 `"输出用表格"` 共享「输出」二字，
被聚成一类（cy 0.301，标签相同时 0.750 稳过线）。
误聚比漏聚危险 —— 它会写进一条错结论。这也是"换编码器"比"修阈值"更根本的原因。

自测：215 + 30 + 17 + 23 + 24 + 14 = **323**。

---

## [0.7.0] - 2026-11-13

跨项目记忆、DSH 插件、以及能看的面板。

### Added

- **作用域（`scope.py`）**：记忆分 `global`（跨项目：偏好/硬规则/通用教训）与
  `proj:owner/repo`（项目内：架构决策/踩过的坑）。默认视图 `auto` = 全局 + 当前项目。
  `--scope global|project|session|all` 自由切换；`promote` 把项目教训升为通用。
  项目标识按 `--project` → `MEMORY_PALACE_PROJECT` → **git remote** → 目录名+哈希 推断，
  所以换目录、换机器、重新 clone 还是同一个项目。
- **沉淀按作用域隔离**：跨项目的碎片不再被聚成一类 —— 否则会结晶出一条"不属于任何项目"的结论。
- **`projects` 命令**：一眼看出记忆分布在哪些作用域、当前在哪个。
- **DSH 插件（`dsh/`）**：host 半注册 5 个工具（context / remember / recall / promote / status），
  按 `defineTool` 契约写、`node --check` 验过语法；`cordis.yml` 给绝对路径与 env 模板。
  面板不嵌 DSH 打包链，走 `GET /panel`（见下），避免随宿主 rc 版本碎掉。
- **`GET /panel`**：`python -m memory_palace.http_api --port 8765` 后浏览器直接开，
  页面按作用域过滤，含作用域分布、剩余寿命分桶、进出账、卡片、自述、墓碑。

### Changed

- `view` / `render` / `context` / `recall` 全部支持 `--scope`；面板与健康度读数跟随视图
  （看着项目视图却报全库熵，会让人误判注意力很集中，其实是被别的项目摊平了）。
- MCP 工具新增 `memory_promote`，`memory_context` / `memory_remember` 支持 `scope`。
- `text_report` 的总数改为"本视图在册数（库共 N）"，不再只报全库。

### Fixed

- `text_report` 引用未定义的 `scopes`（作用域过滤加进去时漏了参数），`view --scope` 直接崩。
- 只有钉死记忆时 `html_report` 的平均剩余寿命除零。
- `scopes_for` 未归一项目名：传裸名 `repo-a` 与节点上存的 `proj:repo-a` 不相等，
  过滤会**静默失效**（表现为"串味"，但不报错）。

自测：215 + 30 + 17 + 23 + 24 = **309**（含 3 条反事实）。

---

## [0.6.0] - 2026-11-13

看完同类项目之后补的三处**语义漏洞**（不是新功能），以及一份调研结论。

### Added

- **双时态（事实有有效期）**：`MemoryNode` 新增 `valid_from` / `valid_to` / `superseded_by` / `supersedes`。
  `remember(..., supersedes=["旧名"])` 让旧事实**到期**而不是被删：默认检索只给当前有效的，
  `recall(as_of=...)` / `recall --as-of 30d|2026-10-01` 回看"当时为真的是什么"。
  失效节点不进重要性账本、不进结构读数、不参与沉淀；不按价值 GC，而是按
  `history_retention_days`（默认 90 天）回收并进墓碑（仍可 restore）。
  这是 Zep/Graphiti 的核心赌注，本库用四个字段 + 一处过滤就拿到，无新依赖。
- **弃答（abstention）**：`context` 在无关查询下不再把"最重要的两条"端上去凑数，
  而是输出「没有相关记忆，不要凭印象补全」，同时**保留钉死的硬规则**（规则与问题无关也要遵守）。
  判据两层：绝对线 `abstain_semantic_floor`（0.30，实测底噪 ≤0.13 / 真匹配 ≥0.5）+ 相对线
  `semantic_relative_floor`。`--status-only` 会显示「无相关记忆」。
- **写入去重**：`remember(..., merge=True)`（CLI / MCP / HTTP 默认开，库层默认关）——
  余弦 ≥0.92 **且长度比 ≥0.85** 才算重复，合并为"强化 + 标签并集"，不新建节点。
- **`docs/COMPETITIVE_ANALYSIS.md`**：Mem0 / Zep / Letta / Cognee / LangMem / TencentDB Agent Memory /
  progressive-memory / mcp-memory-service 的逐项对照，含"为什么拒绝"与"还缺什么"。
- **`tests/test_temporal.py`**：23 项断言，其中 3 条是**反事实**（把有效期判定改恒真、把弃答线降到 0，
  对应断言必须变红）。

### Changed

- `STATE_VERSION` 5→6，`SUPPORTED_STATE_VERSIONS = (4, 5, 6)`。
  `PalaceConfig.from_dict(strict=...)` 现在按快照版本决定严格度：旧快照缺新字段用默认值补齐，
  本版快照出现未知字段仍然拒绝 —— 两种错误不能混为一谈（旧版会把前者也当坏数据，升级即失联）。
- `context` 的"相关"改由语义/标签判，不再用总分（总分含时近分量，人人有份，判不出无关）。
- MCP / HTTP 暴露 `supersedes` 与 `as_of`；`recall` 返回里带 `retired` / `superseded_by`。

### Fixed

- 去重阈值只按余弦会**静默吃掉新内容**：`"单笔不超过总资金三分之一"` 与
  `"…三分之一，且最多同时持三只"` 余弦 0.927，后者补了新约束却会被合并。加长度比（≥0.85）后分开。
- `view` / `status` 里失效记忆混进重要性排序，看起来像"不太重要"，其实只是过期了 —— 现在单列。

---

## [0.5.1] - 2026-11-13

修 CI 抓到的一个真 bug。

### Fixed

- `view` / `text_report` 在「两条记忆剩余寿命完全相同」时抛 `TypeError`
  （`sorted` 落到比较 `MemoryNode` 对象）。排序键改为 `(天数, 名字)`，并补了回归测试。
  这个 bug 只在演示库 60 天版触发 —— 说明「演示能跑」本身就是一条测试。

---

## [0.5.0] - 2026-11-13

三件事：树状注入（省 token）、性能、以及把「演示」做成能见人的东西。

### Added

- **树状注入（`tree.py` + `context --tree`）**：根常驻、命中的枝展开到叶子、其余折叠成一行摘要，
  并显式标注「另有 N 条」。派生自图（沉淀父节点 / 首个标签），不改 state 版本。
  实测 1000 条：同一覆盖面下扁平 1184 字 → 树 603 字（省 49%），耗时 31ms → 8ms。
  诚实说明：窄预算（只要 6 条细节）时树反而略贵，两者是两个档位而不是替代关系。
- **性能**：① 重要性账本加缓存（一次 context 里原本要算 3~4 遍）；
  ② 语义索引改脏标记，不再每次查询哈希全语料；③ 沉淀候选封顶 + 大规模分桶配对。
  `cycle` 在 1000 条上从 1627ms 降到 200ms。
- **`bench.py`**：写入 / 查询 / 注入 / 代谢 / 存储的基准，并与扁平注入对比 token。
- **MCP `--profile slim`**：只暴露 `context / remember / cycle` 三个工具，给「工具定义常驻上下文」
  的宿主（DSH 等）省约 60% 的 schema token；`--list-tools` 可查。
- **`config` 命令**：`memory-palace config --for dsh|claude|codex|cursor|http` 直接打印可粘贴配置，
  绝对路径与转义都算好。
- **DSH（DeepSeek Harness）适配**：`cordis.patch.yml` 写法、`mcp__memory-palace__*` 工具命名、
  环境变量清洗（含 TOKEN/KEY 的会被剥掉，store 必须显式写进 env）、slim 档建议。
- **演示与配图**：`examples/showcase.py` 生成 120 天演化库（全部脱敏，通用 agent 场景）；
  `examples/make_images.py` 生成 README 配图（真实截图，可复现）。

### Changed

- **面板重做**：连续衰减曲线换成「剩余寿命分桶」与「每周进出账」—— 前者在该数据上是一堆平线，
  什么也没说；卡片两列等高、去掉重复的「可靠度 1.00」、徽章不再重复标「钉死」。
- 内部枚举翻译成人话（`flat` → 无梯度、`isolated` → 全是孤岛）。

### Fixed

- 空库的 `structure_health` 判成 `hub`。
- 演示库用「未来的模拟时间」，导致渲染时与重载时的衰减对不上（锚到真实时间）。
- 进出账的「新增」只统计存活节点，墓碑里的历史被漏掉。

---

## [0.4.0] - 2026-11-13

补上**展现层**。起因是一个很实际的问题：记忆系统改的是 prompt 里的一小段，用户看不见 ——
「用了很久」和「根本没生效」，在体验上一模一样。技能加载后必须让人**看见效果**。

### Added

- **状态行**（`context --status-only`）：`[记忆 6/9 · 钉死 2 · 147/900字 · 自述 rev1]`。
  一行交代「这次用了什么」，数字来自命令输出而非印象。
- **终端视图**（`view`）：重要度条形 + **剩余寿命**（`钉死` / `N天` / `极稳（>5年）` / `已过期`）
  + 「N 天内将跌破遗忘线」+ 已遗忘清单。
- **HTML 面板**（`render --out x.html`）：自包含单文件（无 CDN / 无图片 / 跟随系统深浅色），
  含记忆卡片、**未来 N 天衰减曲线**（钉死的是平线）、注意力份额、自述章节、墓碑。
- **`memory_palace/render.py`**：以上三层的实现，纯函数，不写盘（除 render 输出）。
- **技能的「展现契约」**：注入后一行状态、引用记忆时句末角标 `〔记忆·仓位〕`、cycle 后一行摘要、
  用户问「你记得什么」→ `view`、要「看看」→ `render`。写进 SKILL.md，成为硬性输出要求。

### Fixed

- 空库的 `structure_health` 被判成 `hub`（单枢纽）—— 没有任何节点却说它「全靠一个中心撑着」。
  改为 `empty`，并加测试。
- 身份类记忆的剩余寿命直出 `13659284 天`，改为「极稳（>5年）」。

---

## [0.3.0] - 2026-11-13

不做某个平台的专用件。三个接入面覆盖主流 agent：**MCP（stdio）/ HTTP + OpenAPI / CLI + 库**。

### Added

- **MCP 服务（`memory_palace/mcp_server.py`）**：stdio 传输，换行分隔 JSON-RPC（不是 LSP 的
  Content-Length），8 个工具 —— `memory_context / remember / recall / outcome / forget / status /
  explain / cycle`。工具描述写的是「什么时候该用」，工具级失败用 `isError` 交给模型自行纠正，
  协议错误才用 JSON-RPC error。入口 `memory-palace-mcp`。
- **HTTP + OpenAPI（`memory_palace/http_api.py`）**：`/context /remember /recall /outcome /status
  /cycle /health /openapi.json`，OpenAPI 3.0 可被 Dify / Coze / n8n / GPT Actions 直接导入。
  默认只绑 127.0.0.1 且无鉴权，非本机绑定需显式 `--allow-remote`。入口 `memory-palace-http`。
- **`docs/INTEGRATIONS.md`**：Claude Code（`.mcp.json`）、Codex CLI（`config.toml`）、Cursor、
  Cline、Windsurf、Zed、Gemini CLI、Dify、Coze、n8n、LangChain、OpenAI function calling
  的逐条配置；并说明 `inputSchema` 可直接当 function calling 的 `parameters` 用，不必重抄 schema。
- **`AGENTS.md`**：任何读仓库约定的 agent（Codex / Claude Code）都能照着用的时序与硬约束。
- **`tests/test_integrations.py`**：15 项协议层契约（握手 / 工具清单 / 错误语义 / OpenAPI 形状 / 404 与 400 的区分）。

### Changed

- CLI 的 `context` 排序改为三档：钉死永远在 → 两条背景 → `0.6×语义 + 0.4×重要性`。
  修掉「无关查询下硬规则被语义门槛挡掉」的问题 —— 那是规则变成资料的典型退化。
- `pyproject` 增加 `memory-palace-mcp` / `memory-palace-http` 两个入口。

### Fixed

- `context` 在语义门槛较高时只返回 1~2 条，导致注入块几乎没有信息量。

---

## [0.2.0] - 2026-11-13

从「库」到「agent 能用的工具」。核心判断：agent 缺的不是相似文档，而是
「我该记住什么、该放下什么、这条还能不能信」，所以接口不是 `search() -> chunks`，
而是 `context(task, budget) -> 一小块可注入的文本`。

### Added

- **CLI（`memory_palace/cli.py`）**：`init / remember / recall / context / cycle / status / explain /
  outcome / pin / forget / style`。默认存储 `~/.memory-palace/store.json`，
  可用 `--store` 或 `MEMORY_PALACE_STORE` 覆盖；`pip install` 后入口为 `memory-palace`。
- **`context` 三档排序**：钉死记忆永远在（规则不是资料）→ 两条背景（当前最重要）→
  其余按 `0.6×语义 + 0.4×重要性` 混合。带可信度警示、风格约束、预算用量，
  `--json` 可拿结构化结果。
- **`MemoryPalace.pin() / drop()`**：用户主权的两个开关。`drop` 走与 GC 相同的清理路径
  （墓碑 → 出图 → 出语义索引 → 剪逻辑边），不留幽灵。
- **`docs/AGENT_INTEGRATION.md`**：三种接入时机（注入 / 固化 / 反馈）、多库划分、成本，以及**不该用它**的场景。
- **`tests/test_cli.py`**：16 项契约自测，CI 增加一步。

### Changed

- `retrieval_coverage` 在「一次检索都没有」时返回 1.0 而不是 0.0，并新增 `retrieval_attempts` 指标；
  `retrieval_narrow` 规则要求 `retrieval_attempts >= 5` 才触发。
  **修掉的真实副作用**：新库第一轮 `cycle` 会误判「召回路径过窄」，去下调语义门槛。

### Fixed

- 空库/新库跑第一轮 `cycle` 会凭空产生一条自修正（零样本被当成零覆盖率）。

---

## [0.1.0] - 2026-11-13

首个公开版本。核心是「记忆的生命周期」，不是「检索的一次调用」。

### Added

- **核心（`core.py`）**：幂等衰减（权重 = 锚点 + 时间的纯函数，GC 跑 1 次与 100 次账本一致）、
  共现图、原子持久化（tempfile + fsync + `os.replace` + 目录 fsync，含 Windows 路径）、
  状态版本 5，兼容读取 v4 快照。
- **情感（`affect.py`）**：潮水（唤醒）/ 河床（峰值唤醒）/ 失真代价三量分离；
  `fidelity` 随峰值唤醒下降且不随时间恢复；评价器从文本词表与运行时结果推断情绪，带 `source` / `drivers`。
- **逻辑（`logic.py`）**：有向带类型关系、传递闭包、承重打折、矛盾沿推理链传播、扩散激活带扇出惩罚。
- **沉淀（`consolidation.py`）**：标签 Jaccard 与语义余弦双通道聚类，单向门（结晶后碎片降级 trace）。
- **选择（`value.py`）**：重要性四分量（强度 / 情绪余温 / 逻辑凭据 / 叙事地位）、
  两套遗忘律（结论按价值、碎片按物理强度）、固定注意力预算、每条判决带理由；
  新增**结构熵**（度熵 / 孤立占比 / 连通分量 / 碎片度），与价值健康分开判。
- **自述（`narrative.py`）**：投影而非节点；章节与叙事弧；条目级不可靠标注；叙事地位随时间退潮。
- **人格（`persona.py`）**：基座 / 人格 / 人设 / 修订四层；基座只读（修宪需 reason + force）、
  人格带惯性与包络硬夹、人设可换可回滚、漂移判据 = 距离 × 速度。
- **记忆模型（`memory_model.py`）**：五种记忆五种寿命；工作记忆超容量**降级**不删除；身份记忆不参与遗忘。
- **反思（`reflection.py`）**：指标化回看，只提建议；新增结构（孤岛 / 碎裂 / 单枢纽）与注意力未覆盖率读数。
- **自修正（`selfcorrect.py`）**：12 个白名单参数 + 逐参数值域 + 单次步长 ≤30% + 冷却 + Patch 可回滚；
  新增观察窗采样，**样本比观察窗还旧时拒绝判决**；判据要求变化幅度跑赢噪声带；同参数回滚 ≥2 次永久收权。
- **语义（`semantics.py`）**：多义词按上下文分配概念份额（不再硬编码归属）；
  概念词从语料学习（df 区间 + 与同概念 ≥2 个锚点同篇 + 提升度 + 纯净度）；
  `semantic_explain()` 可查「为什么归到这个概念」。索引状态是图内容的纯函数，跨进程逐条一致。
- **示例与自测**：`examples/quickstart.py`；`memory_palace/demo.py` 21 幕剧本，215 项断言，
  18 条突变审计（故意弄坏实现，断言必须变红）。

### Changed

- `MemoryPalace.remember(valence=None, arousal=None)`：不传情绪时走评价器，而不是默认全零。
  **行为变化**：文本含情绪词（亏 / 突破 / 恐慌…）的记忆会自带情绪，进而更难忘、也更不可靠。
- `MemoryNode.narrative_hits` 语义收窄为「审计用累计次数」；价值判断改读会退潮的 `central`。
- `atomic_write_json` 返回布尔值（目录项是否持久化），不再静默降级。
- `STATE_VERSION` 4 → 5。

### Fixed

- 没有自述就 `reflect()` 会因 `float(None)` 崩溃。
- Windows 上目录 fsync 永远失败并被静默吞掉（改为 `CreateFileW + FlushFileBuffers`）。
- `replace` 与临时文件清理同时失败时会残留 1 个 `.palace-*.tmp`（改为重试 + 下次写盘回收）。
- 叙事地位单调饱和：讲过一次的记忆终身「重要」。
- `资金` 在「板块资金接力」语境里被误判为仓位概念。

### Known Limitations

见 [README · 成熟度自评](README.md#成熟度自评先说不好听的)。要点：语义层不是 embedding；
测试是作者自证；自修正的「变好」仍是单指标；单写者假设。
