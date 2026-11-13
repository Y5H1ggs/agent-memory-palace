# Changelog

所有值得写下来的变更都在这里。格式参照 [Keep a Changelog](https://keepachangelog.com/)，版本号遵循 [SemVer](https://semver.org/lang/zh-CN/)。

## 为什么第一个公开版本是 0.1.0 而不是 1.0.0

SemVer 里 `1.0.0` 的含义是「公开 API 已稳定，可以放心依赖」。这个项目现在还不满足：

- 没有任何外部使用者，API 是按内部需要长的（`run_cycle` 的返回结构、`MemoryPalace` 的构造参数都可能变）；
- 语义层是概念种子 + 字符 bigram + 语料学习，没有独立评测集，召回上限未知；
- 只有单写者假设，没有跨进程文件锁。

**升到 1.0.0 的条件**（四条全部满足）：外部使用者 ≥ 1、CI 覆盖 Linux/Windows/macOS、语义层有公开评测集与基线数字、跨进程写锁落地。

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
