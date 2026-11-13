"""test_cli.py - CLI 的端到端断言（不依赖 pytest，也能被 pytest 收集）。

    python tests/test_cli.py
    pytest -q

为什么单独测 CLI：agent 是通过命令行碰这套记忆的，命令行坏了，库再对也没用。
这里只测「契约」——退出码、输出形状、跨进程持久化、以及两个曾经真的出过问题的点：
  · 空库第一轮不该触发自修正（覆盖率在零样本时曾被算成 0%，会去下调语义门槛）
  · 显式丢弃必须同时清掉墓碑/语义索引/逻辑边，不能留幽灵
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV = dict(os.environ)


def run(store: str, *args: str) -> subprocess.CompletedProcess:
    env = dict(ENV)
    env["MEMORY_PALACE_STORE"] = store
    env["PYTHONPATH"] = ROOT + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "memory_palace.cli", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=ROOT,
        timeout=120,
    )


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{label} {detail}".strip())
    print(f"  [PASS] {label}" + (f"  · {detail}" if detail else ""))


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="mp-cli-") as tmp:
        store = os.path.join(tmp, "store.json")

        init = run(store, "init")
        check("init 成功并给出路径", init.returncode == 0 and os.path.exists(store), init.stdout.strip())

        rule = run(store, "remember", "禁止绝对化措辞，所有判断都带概率",
                   "--tags", "硬规则,措辞", "--tier", "core", "--pin")
        check("remember 回传名字（agent 之后要用它）", "已记住 m:" in rule.stdout, rule.stdout.strip())
        rname = rule.stdout.split("已记住 ")[1].split("｜")[0]

        again = run(store, "remember", "禁止绝对化措辞，所有判断都带概率")
        check("同一内容重复写入是幂等的（内容哈希做名字）", "已存在同名记忆" in again.stdout)

        loss = run(store, "remember", "追高之后逆板块阴跌，亏了三个点，很后悔", "--tags", "复盘,教训")
        check("情绪由文本推断，并标出来源", "v=-0" in loss.stdout and "text" in loss.stdout, loss.stdout.strip())

        run(store, "remember", "半凯利仓位：单笔不超过总资金三分之一", "--tags", "仓位")

        recall = run(store, "recall", "仓位怎么分配")
        check("语义召回命中仓位那条", "半凯利仓位" in recall.stdout, recall.stdout.strip().splitlines()[0])

        # ★ 核心契约：不管问什么，钉死的硬规则都必须出现
        ctx = run(store, "context", "--query", "周末去哪玩", "--budget", "400")
        check(
            "context 在无关查询下仍注入钉死规则（规则不是资料）",
            "禁止绝对化措辞" in ctx.stdout and "钉死" in ctx.stdout,
            ctx.stdout.splitlines()[0],
        )
        check("context 自报预算用量", "字，语义检索" in ctx.stdout, ctx.stdout.splitlines()[0])

        wide = run(store, "context", "--budget", "400", "--json")
        payload = json.loads(wide.stdout)
        check("context --json 结构完整", {"items", "warnings", "used", "budget"} <= set(payload), str(list(payload)))
        check("预算内不超支", payload["used"] <= payload["budget"] + 200, f"used={payload['used']}")

        before = json.loads(run(store, "recall", "--name", rname, "--json").stdout)[0]["importance"]["weight"]
        outcome = run(store, "outcome", rname, "contradicted")
        after = json.loads(run(store, "recall", "--name", rname, "--json").stdout)[0]["importance"]["weight"]
        check(
            "结果反馈只改情绪、不强化记忆",
            outcome.returncode == 0 and "outcome" in outcome.stdout and before == after,
            f"权重 {before}→{after}",
        )
        check("未知记忆给出非零退出码", run(store, "outcome", "m:nope", "loss").returncode == 1)

        run(store, "pin", rname, "--unpin")
        unpinned = json.loads(run(store, "status", "--json").stdout)
        check("解钉后不再是 pinned", unpinned["nodes"] == 3, f"nodes={unpinned['nodes']}")

        # ★ 空库第一轮不该触发自修正（覆盖率零样本的旧 bug）
        fresh_store = os.path.join(tmp, "fresh.json")
        run(fresh_store, "remember", "一条普通记录")
        fresh = run(fresh_store, "cycle", "--json")
        cycle_payload = json.loads(fresh.stdout)
        check(
            "新库第一轮不产生自修正（没有样本就没有症状）",
            cycle_payload["selfcorrect"]["applied"] == [],
            str(cycle_payload["selfcorrect"]),
        )

        # ★ 显式丢弃必须清干净
        run(store, "forget", rname)
        gone = json.loads(run(store, "status", "--json").stdout)
        hits = run(store, "recall", "禁止绝对化")
        check(
            "forget 后图与检索都不再命中（无幽灵）",
            gone["nodes"] == 2 and "禁止绝对化" not in hits.stdout and gone["tombstone"] >= 1,
            f"nodes={gone['nodes']} tombstone={gone['tombstone']}",
        )

        style = run(store, "style", "observe", "concise", "输出尽量精简", "--sign", "+1")
        active = run(store, "style", "active")
        check("风格证据可写入并读回", style.returncode == 0 and "生效风格" in active.stdout, active.stdout.strip())

        bad = run(store, "style", "observe")
        check("参数缺失给出用法错误退出码", bad.returncode == 2, str(bad.returncode))

    print("\nCLI 契约自测通过 ✅")


def test_cli_contract() -> None:
    main()


if __name__ == "__main__":
    main()
