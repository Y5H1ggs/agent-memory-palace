#!/usr/bin/env python
"""make_portable_bundle.py - 拼一个「解压即开工」的整体包。

为什么要一个「整体包」而不是把代码 zip、技能 zip、数据 zip 分开给：

分开给的时候，人在新机器上做的第一件事是「把三个东西分别放到哪」——
step 0 就已经开始出错了。整体包把这一步变成「解压，然后 python verify.py」。

结构是刻意的：

    app/      代码（git archive，不含 .git / __pycache__）
    skill/    WorkBuddy 技能（可直接拷进 ~/.workbuddy/skills/）
    data/     记忆导出（可选，--no-data 跳过）
    verify.py 分层自检
    00-READ-ME-FIRST.md  一页纸

    python tools/make_portable_bundle.py
    python tools/make_portable_bundle.py --no-data --out dist/x.zip
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SKILL = os.path.expanduser("~/.workbuddy/skills/memory-palace")

READ_ME = """# 记忆宫殿 · 解压即开工

这是一个**零第三方依赖**的长期记忆库。整包里已经放好了代码、技能、记忆导出和自检脚本。

## 1. 三分钟跑起来

```bash
cd agent-memory-palace
python verify.py            # 先看它报什么
```

`verify.py` 会分五层告诉你「哪一层通了、哪一层没通」，比直接跑 demo 有信息量得多。

然后进代码目录：

```bash
cd app
python -m memory_palace.demo                              # 20 条断言，验证代码本身
python -m memory_palace.cli import ../data/memory-export.zip   # 导入附带的记忆（会先备份）
python -m memory_palace.cli status                        # 看条数对不对
```

> 想直接用 `memory-palace` 这个短命令：在本目录跑一次 `python -m pip install -e .`。
> 不装也能用，只是每次要多打 `-m memory_palace.cli`。

## 2. 要不要开语义通道

**有 ollama 的机器**（推荐）：

```bash
ollama pull bge-m3
ollama serve                     # 如果还没跑
cd .. && python verify.py        # 「稠密通道」那一行会从 ! 变成 ✓
```

不用改任何配置 —— `embedding_provider` 默认 `auto`，它自己探测 `11434`。

**没有 ollama**：走纯词法。10k 条以内查询 5–20ms，够用；
只是「换个说法」的查询召不回来（比如「止盈」和「落袋为安」）。

## 3. 目录里都是什么

| 路径 | 是什么 |
|---|---|
| `app/` | 全部代码 + 文档 + 测试（`docs/PORTABLE.md` 讲迁移，`docs/DEBUG.md` 讲排障） |
| `skill/memory-palace/` | WorkBuddy 技能。拷到 `~/.workbuddy/skills/` 并把 `.home` 改成这里的 `app/` 绝对路径 |
| `data/memory-*.zip` | 记忆导出（含 sha256），用 `import` 恢复 |
| `verify.py` | 分层自检：代码 / 库 / 索引 / 检索 / 注入 |

## 4. 接宿主：三选一，别同时开

| 宿主 | 怎么接 | 注意 |
|---|---|---|
| WorkBuddy | 拷 `skill/` 到技能目录 | `.home` 写 `app/` 的绝对路径 |
| Claude Code / Cursor / Codex | `python -m memory_palace.cli config --for claude` 生成 MCP 配置 | 合并进 `mcp.json`，别覆盖已有的 |
| DSH | `dsh web --patch app/dsh/cordis.yml` | 先替换 `cordis.yml` 里的 `__REPO__` / `__HOME__` |

**同时开两套会写出两份互不相干的记忆**，表现是「今天记得、明天忘了」。

## 5. 出问题先看这里

```bash
python verify.py --full     # 含 demo 断言
```

- 卡在「代码可导入」→ `docs/DEBUG.md` 第 0 层
- 卡在「记忆库文件」→ 第 1 层
- 写得进、查不到 → 第 2 层（多半是作用域不对）
- 查得到、注入是空 → 第 3 层
- 注入有内容、模型却不遵守 → 第 4 层（宿主没真的用上）

别在库打不开的时候先接宿主 —— 那时你分不清是库坏了还是接错了。
"""


def _run(cmd: list[str], cwd: str = ROOT) -> None:
    subprocess.run(cmd, cwd=cwd, check=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="make-portable-bundle")
    parser.add_argument("--out", default="", help="输出 zip 路径（默认 dist/agent-memory-palace-<版本>-full.zip）")
    parser.add_argument("--no-data", action="store_true", help="不打包记忆导出")
    parser.add_argument("--no-skill", action="store_true", help="不打包技能")
    args = parser.parse_args(argv)

    version = subprocess.run(
        [sys.executable, "-c", "import sys;sys.path.insert(0,'.');import memory_palace;print(memory_palace.__version__)"],
        cwd=ROOT, capture_output=True, text=True,
    ).stdout.strip() or "0.0.0"

    out = args.out or os.path.join(ROOT, "dist", f"agent-memory-palace-{version}-full.zip")
    out = os.path.abspath(out)
    os.makedirs(os.path.dirname(out), exist_ok=True)

    stage = tempfile.mkdtemp(prefix="mp-bundle-")
    bundle = os.path.join(stage, "agent-memory-palace")
    app = os.path.join(bundle, "app")
    os.makedirs(app, exist_ok=True)

    # 1) 代码：用 git archive，天然排除 .git / __pycache__ / 未提交的临时文件
    tar = os.path.join(stage, "app.zip")
    _run(["git", "archive", "--format=zip", "-o", tar, "HEAD"])
    with zipfile.ZipFile(tar) as z:
        z.extractall(app)
    os.remove(tar)

    # 2) 技能
    if not args.no_skill and os.path.isdir(SKILL):
        dst = os.path.join(bundle, "skill", "memory-palace")
        shutil.copytree(
            SKILL, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        # `.home` 存的是**制作机器**上的绝对路径。搬到新机器后它必然失效，
        # 而失效的表现是「找不到 memory_palace 库」——一个看起来像代码坏了的假故障。
        # 与其留一个错的，不如删掉，让使用者显式写一次。
        stale = os.path.join(dst, ".home")
        if os.path.exists(stale):
            os.remove(stale)
        with open(os.path.join(dst, "SETUP.txt"), "w", encoding="utf-8") as fh:
            fh.write(
                "这个技能包还需要一步：告诉它代码在哪。\n\n"
                "1) 把本文件删掉\n"
                "2) 在同一目录新建 .home（注意前面有个点），内容一行：app/ 的绝对路径\n\n"
                "例：\n"
                "  D:/tools/agent-memory-palace/app\n\n"
                "写完后再跑：python scripts/mp.py context --query 测试 --budget 200\n"
                "如果报「找不到 memory_palace 库」，就是这一行写错了（它不猜路径）。\n"
            )

    # 3) 记忆导出
    if not args.no_data:
        data_dir = os.path.join(bundle, "data")
        os.makedirs(data_dir, exist_ok=True)
        target = os.path.join(data_dir, "memory-export.zip")
        proc = subprocess.run(
            [sys.executable, "-m", "memory_palace.cli", "export", target],
            cwd=ROOT, capture_output=True, text=True,
        )
        if proc.returncode != 0:
            print("（没有导出记忆：", (proc.stderr or "").strip()[:80], "）")
            os.rmdir(data_dir)

    # 4) 自检脚本 + 一页纸
    shutil.copy2(os.path.join(HERE, "verify_portable.py"), os.path.join(bundle, "verify.py"))
    with open(os.path.join(bundle, "00-READ-ME-FIRST.md"), "w", encoding="utf-8") as fh:
        fh.write(READ_ME)

    # 5) 打包
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for base, dirs, files in os.walk(bundle):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for name in files:
                if name.endswith(".pyc"):
                    continue
                full = os.path.join(base, name)
                z.write(full, os.path.relpath(full, stage))

    count = len(zipfile.ZipFile(out).namelist())
    size = os.path.getsize(out) / 1024 / 1024
    print(f"✓ {out}")
    print(f"  {count} 个文件 · {size:.1f} MB · 版本 {version}")
    print("  解压后先跑：python verify.py")
    shutil.rmtree(stage, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
