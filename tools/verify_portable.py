#!/usr/bin/env python
"""verify.py - 在陌生机器上判断「到底哪一层没通」。

它不是为了「证明安装成功」，而是为了**定位**。同一句「记不住」，可能卡在四个完全
不同的地方，修法互不相干：

    代码导入失败  → 路径 / Python 版本
    库打不开      → 文件坏了 / 版本不兼容 / 放在网络盘
    检索没结果    → 索引没建 / 阈值不对 / 作用域不匹配
    注入是空的    → 预算为 0 / 全是无关条目（弃答是对的）

所以这里按层检查，每层给一条判据和一句「如果不通过该看什么」。

    python verify.py                 # 基础检查（约 1 秒）
    python verify.py --full          # 加跑 demo 的 20 条断言（约 3 秒）
    python verify.py --store /path/to/store.sqlite
    python verify.py --json
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# 两种位置都要能跑：打包后它在包根（旁边是 app/），在仓库里它在 tools/（上一级才是根）
APP = HERE
for candidate in (os.path.join(HERE, "app"), HERE, os.path.dirname(HERE)):
    if os.path.isdir(os.path.join(candidate, "memory_palace")):
        APP = candidate
        break
sys.path.insert(0, APP)

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
_rows: list[dict[str, str]] = []


def record(name: str, state: str, detail: str = "", fix: str = "") -> None:
    _rows.append({"check": name, "state": state, "detail": detail, "fix": fix})
    if not args_json:
        mark = {PASS: "✓", WARN: "!", FAIL: "✗"}[state]
        print(f"  [{mark}] {name:<26} {detail}")
        if state == FAIL and fix:
            print(f"      → {fix}")


args_json = False


def main(argv: list[str] | None = None) -> int:
    global args_json
    parser = argparse.ArgumentParser(prog="verify", description="搬到新机器后的分层自检")
    parser.add_argument("--full", action="store_true", help="额外跑 demo 的 20 条断言")
    parser.add_argument("--store", default="", help="指定记忆库路径（默认 ~/.memory-palace/store.sqlite）")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    args_json = args.json

    if not args_json:
        print("记忆宫殿 · 环境自检")
        print("=" * 56)

    # ── 第 0 层：Python 与代码 ──
    version = ".".join(str(v) for v in sys.version_info[:3])
    if sys.version_info >= (3, 10):
        record("Python 版本", PASS, version)
    else:
        record("Python 版本", FAIL, version, "需要 3.10+；换解释器或升级")

    try:
        import memory_palace  # noqa: PLC0415

        record("代码可导入", PASS, f"memory_palace {memory_palace.__version__}")
    except Exception as exc:  # noqa: BLE001 - 任何导入错误都要如实报
        record("代码可导入", FAIL, str(exc)[:80], "确认 app/memory_palace/ 存在，且没有改名")
        _finish()
        return 1

    # ── 第 1 层：库能不能开 ──
    from memory_palace.palace import MemoryPalace  # noqa: PLC0415

    store = os.path.expanduser(
        args.store or os.environ.get("MEMORY_PALACE_STORE") or "~/.memory-palace/store.sqlite"
    )
    store = os.path.abspath(store)
    if not os.path.exists(store):
        record(
            "记忆库文件", WARN, f"不存在：{store}",
            "正常（新机器还没导入）。要搬旧记忆：memory-palace import data/memory-*.zip",
        )
        palace = None
    else:
        try:
            palace = MemoryPalace(path=store)
            size = os.path.getsize(store) / 1024
            record("记忆库文件", PASS, f"{size:.0f} KB · {len(palace.graph)} 条记忆")
        except Exception as exc:  # noqa: BLE001
            record(
                "记忆库文件", FAIL, str(exc)[:90],
                "跑 memory-palace fsck；版本太旧就先 import 一遍（导入会做迁移）",
            )
            palace = None

    # ── 第 2 层：索引与检索 ──
    if palace is not None:
        try:
            palace.semantics.sync(palace.graph)
            indexed = len(palace.semantics)
            if indexed == len(palace.graph):
                record("词法索引", PASS, f"{indexed} 条已建")
            else:
                record("词法索引", WARN, f"{indexed}/{len(palace.graph)}",
                       "跑一次 recall 会自动补建；仍不齐就看有没有坏内容")
        except Exception as exc:  # noqa: BLE001
            record("词法索引", FAIL, str(exc)[:80], "跑 memory-palace fsck")

        try:
            probe = palace.recall(query="测试", limit=3)
            record(
                "检索可用", PASS, f"「测试」命中 {len(probe)} 条",
            ) if probe else record(
                "检索可用", WARN, "「测试」没有命中",
                "这可能是**正确**的弃答（库里确实没有相关记忆）；换一个库里真有的词再试",
            )
        except Exception as exc:  # noqa: BLE001
            record("检索可用", FAIL, str(exc)[:80], "带上库路径跑 memory-palace explain \"测试\"")

        # ── 第 3 层：语义通道（可选）──
        embedder = getattr(palace.semantics, "embedder", None)
        if embedder is None:
            record(
                "稠密通道", WARN, "未启用（走纯词法）",
                "要语义召回：ollama serve + ollama pull bge-m3，然后 memory-palace embed --check",
            )
        else:
            try:
                from memory_palace.embedding import self_check  # noqa: PLC0415

                result = self_check(embedder)
                if result["ok"]:
                    record("稠密通道", PASS, f"{result['name']} · 差距 {result['margin']}")
                else:
                    record("稠密通道", FAIL, f"同义与无关分不开（差距 {result['margin']}）",
                           "换中文模型（bge-m3 / bge-small-zh），或把 hybrid_weight 降到 0.2 以内")
            except Exception as exc:  # noqa: BLE001
                record("稠密通道", FAIL, str(exc)[:80], "memory-palace embed --check")
    else:
        record("词法索引", WARN, "跳过（没有库）")
        record("检索可用", WARN, "跳过（没有库）")
        record("稠密通道", WARN, "跳过（没有库）")

    # ── 第 4 层：注入 ──
    if palace is not None and len(palace.graph):
        try:
            from memory_palace.cli import build_context  # noqa: PLC0415

            ctx = build_context(palace, budget=400)
            total = len(ctx["items"]) + len(ctx.get("index") or [])
            if total:
                record("注入产出", PASS, f"{len(ctx['items'])} 条全文 + {len(ctx.get('index') or [])} 条索引")
            else:
                record("注入产出", WARN, "空（候选为空或预算为 0）", "确认 --budget 不是 0")
        except Exception as exc:  # noqa: BLE001
            record("注入产出", FAIL, str(exc)[:80], "memory-palace context --budget 400")

    # ── 第 5 层：demo（可选）──
    if args.full:
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "memory_palace.demo"],
                cwd=APP, capture_output=True, text=True, timeout=120,
            )
            tail = [ln for ln in (proc.stdout or "").splitlines() if "断言" in ln]
            if proc.returncode == 0:
                record("demo 断言", PASS, tail[-1] if tail else "全绿")
            else:
                record("demo 断言", FAIL, (tail[-1] if tail else f"退出码 {proc.returncode}"),
                       "把 stderr 贴出来；这是代码本身的问题，不是配置")
        except subprocess.TimeoutExpired:
            record("demo 断言", FAIL, "超时 120s", "机器太慢或卡在模型下载")

    _finish()
    return 0 if not any(r["state"] == FAIL for r in _rows) else 1


def _finish() -> None:
    fails = [r for r in _rows if r["state"] == FAIL]
    warns = [r for r in _rows if r["state"] == WARN]
    if args_json:
        print(json.dumps(
            {"ok": not fails, "checks": _rows, "fail": len(fails), "warn": len(warns)},
            ensure_ascii=False, indent=2,
        ))
        return
    print("=" * 56)
    missing_store = any(
        r["check"] == "记忆库文件" and r["state"] == WARN for r in _rows
    )
    if fails:
        print(f"结论：{len(fails)} 项没通过，先按上面的 → 处理，别急着接宿主。")
    elif missing_store:
        # 「库不存在」和「有库但没开语义」是两件事：前者什么都干不了，
        # 后者已经能用了。混成一句「可以开始用」会让人以为已经好了。
        print("结论：代码没问题，但还没有记忆库 —— 先 `import data/` 里的导出，或 `remember` 一条。")
    elif warns:
        print(f"结论：可以开始用（{len(warns)} 项是提示，不是故障）。")
    else:
        print("结论：全绿，可以开始用。")


if __name__ == "__main__":
    raise SystemExit(main())
