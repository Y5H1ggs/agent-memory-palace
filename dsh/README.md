# DSH 插件（DeepSeek Harness）

把 `memory-palace` 接进 DSH。**这是一个适配层，不是第二套实现** ——
它把模型的一次工具调用翻译成 `python -m memory_palace.cli ...`，
所以记忆的写法、衰减、弃答、作用域判定全部与本仓库的库/CLI 完全一致。

```
dsh/
  package.json    插件元信息（type: module，engines.node >= 20）
  src/index.js    host 半：注册 5 个工具（context / remember / recall / promote / status）
  cordis.yml      挂载配置（绝对路径 + env）
```

## 装

```bash
# 1) 先确认 CLI 能用
cd /path/to/memory-palace
python -m memory_palace.cli --help

# 2) 把 cordis.yml 里的 __REPO__ / __HOME__ 换成你的真实路径
#    （macOS/Linux 用正斜杠；Windows 也建议用正斜杠，YAML 双引号里反斜杠是转义符）

# 3a) 临时加载
dsh web --patch ./dsh/cordis.yml

# 3b) 或永久生效：并入 ~/.dsh/profiles/<profile>/cordis.patch.yml，保存后约 1 秒热重载
```

启动后终端会打印 `[memory-palace] plugin loaded`（若你的 DSH 版本会打印插件日志），
或在会话里让模型「看看你记得什么」，它应当调用 `memory_status`。

## 五个工具

| 工具 | 什么时候用 | 关键参数 |
|---|---|---|
| `memory_context` | 任务开始、用户提「上次」「按老规矩」 | `scope`（auto/global/project/all）、`budget`、`tree` |
| `memory_remember` | 用户说「记住」「以后都这样」 | `scope`（默认 project）、`tier`、`pin`、`supersedes` |
| `memory_recall` | 要精确找某条、或问「当时怎么定的」 | `as_of`（`30d` / `2026-10-01`） |
| `memory_promote` | 某个项目的教训其实是通用的 | `name` |
| `memory_status` | 用户问「你记得什么」 | — |

## 作用域：跨项目与单项目之间

DSH 里这个插件会被多个工作区共用一份 store。记忆分两层：

- `global` —— 用户偏好、硬规则、通用教训。**换项目也在**。
- `project:xxx` —— 这个项目的架构决策、踩过的坑。**只在它里面出现**。

默认 `scope=auto` 就是两者相加。要强制单项目视图：`memory_context(scope="project")`。
项目标识按 `MEMORY_PALACE_PROJECT` → git remote（`proj:owner/repo`）→ 目录名+哈希 依次推断，
所以同一个仓库换目录、重新 clone 仍然是同一个项目。

## 面板（好看的那部分）

面板不嵌在 DSH 的 web 打包链里，而是走一个本地只读页面 —— 这是刻意的：

```bash
python -m memory_palace.http_api --port 8765
# 浏览器打开 http://127.0.0.1:8765/panel
```

页面是**自包含单文件**（无 CDN、无图片、跟随系统深浅色），含作用域分布、剩余寿命分桶、
每周进出账、记忆卡片、自述、墓碑。好处是：不依赖 DSH 版本，升级不会把 UI 搞碎；
坏处是要多起一个进程。

> 为什么不写 client 半（`exports['./client']` + `dsh.client`）：
> 那需要按 loader 期望产出 lazy-CJS factory bundle，我在开发机上**没有 DSH 可验证**，
> 写一份跑不通的打包配置比不写更糟。要挂进 slot 的话，最小做法是在你的插件里注册一个
> iframe 指向上面的 `/panel`，20 行以内。

## 已知限制（如实写）

- **未在真实 DSH 中验证**：本插件是按 `@deepseek-ai/dsh-tools` 的 `defineTool` 契约写的
  （`parameters` / `output.schema` / `execute` / `exec.signal`），并用 `node --check` 验过语法，
  但没跑过宿主。DSH 是 developer preview，字段名在 rc 之间改过。
- 每次工具调用会 fork 一个 python 进程（约 100–150 ms）。会话里一次 `context` 就够，
  不要每轮都调 `cycle`。
- 单写者：DSH 与别的宿主同时写同一个 store 会互相覆盖（写是原子的，锁不跨进程）。
  多宿主场景请给每个宿主配不同的 `MEMORY_PALACE_STORE`。
