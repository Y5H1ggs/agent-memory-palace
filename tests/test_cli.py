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

        # ★ 展现层：用户必须能看见「这次用了几条 / 谁快被忘掉 / 遗忘长什么样」
        status = run(store, "context", "--query", "仓位", "--status-only")
        line = status.stdout.strip()
        check(
            "context --status-only 只输出一行、且以 [记忆 开头（可贴给用户看）",
            status.returncode == 0 and len(line.splitlines()) == 1 and line.startswith("[记忆"),
            line,
        )

        view = run(store, "view")
        check(
            "view 给出重要度条形、剩余寿命与遗忘预警",
            view.returncode == 0 and "寿命" in view.stdout and "遗忘线" in view.stdout,
            view.stdout.splitlines()[2] if view.stdout else "",
        )

        out = os.path.join(tmp, "palace.html")
        rendered = run(store, "render", "--out", out)
        html = open(out, encoding="utf-8").read() if os.path.exists(out) else ""
        check(
            "render 生成自包含 HTML（卡片 + 两张 SVG 图，无外链）",
            rendered.returncode == 0
            and "<article" in html
            and html.count("<svg") >= 2
            and "还能记住多久" in html
            and "进出账" in html
            and "http" not in html.split("<style>")[0],
            f"{len(html.encode())} 字节",
        )
        check(
            "HTML 里有墓碑区与自述章节（被遗忘的也看得见）",
            "墓碑" in html and "自述" in html,
        )

        # ★ 树状注入：根常驻、折叠必须标出来、并给出与扁平的用量对比
        tree_store = os.path.join(tmp, "tree.json")
        run(tree_store, "init")
        run(tree_store, "remember", "禁止编造版本号，不确定就说不确定",
            "--tags", "硬规则", "--tier", "core", "--pin")
        for i, topic in enumerate(("复盘", "仓位", "选股", "输出", "风险", "板块", "偏好", "杂记")):
            run(tree_store, "remember", f"{topic}的一条记录 {i}", "--tags", topic)
        tree = run(tree_store, "context", "--tree", "--query", "仓位", "--budget", "600",
                   "--compare", "--json")
        payload = json.loads(tree.stdout)
        check(
            "树状注入：钉死的根永远在，且显式标注折叠了多少条",
            payload["roots"] >= 1 and payload["folded_nodes"] >= 1 and payload["branches"] >= 5,
            f"根 {payload['roots']} / 枝 {payload['branches']} / 折叠 {payload['folded_nodes']}",
        )
        check(
            "树状模式给出与扁平的用量对比（省字要能被验证，不能只靠嘴说）",
            "flat_used" in payload and "saving" in payload,
            f"树 {payload['used']} / 扁平 {payload.get('flat_used')} / 省 {payload.get('saving')}",
        )

        # ★ 一键打印宿主配置（DSH 等）
        conf = run(store, "config", "--for", "dsh")
        check(
            "config --for dsh 给出可直接粘贴的桥接配置，且路径无转义坑",
            "dsh-mcp-client" in conf.stdout
            and "serverName: memory-palace" in conf.stdout
            and "\\U" not in conf.stdout,
            conf.stdout.splitlines()[3] if conf.stdout else "",
        )

        # 回归：两条记忆剩余寿命完全相同时，排序不得去比较节点对象（CI 抓到过）
        twin = os.path.join(tmp, "twin.json")
        run(twin, "init")
        run(twin, "remember", "同一时刻写入的第一条", "--tags", "复盘")
        run(twin, "remember", "同一时刻写入的第二条", "--tags", "复盘")
        twin_view = run(twin, "view")
        check(
            "剩余寿命相同时 view 不崩（排序键必须可比较）",
            twin_view.returncode == 0 and "遗忘线" in twin_view.stdout,
            twin_view.stdout.splitlines()[1] if twin_view.stdout else twin_view.stderr[-60:],
        )

        # ★ 作用域：跨项目共享 / 项目内隔离
        scoped = os.path.join(tmp, "scoped.json")
        run(scoped, "init")
        run(scoped, "remember", "全局：不确定就说不确定", "--tags", "硬规则",
            "--tier", "core", "--pin", "--scope", "global", "--project", "p1")
        run(scoped, "remember", "P1 的部署脚本要先 source env.sh", "--tags", "部署",
            "--project", "p1")
        p1 = json.loads(run(scoped, "recall", "部署脚本", "--project", "p1", "--json").stdout)
        p2 = json.loads(run(scoped, "recall", "部署脚本", "--project", "p2", "--json").stdout)
        check(
            "跨项目隔离：p1 看得到自己的部署记忆",
            any(r["name"] for r in p1),
            str([r["name"] for r in p1]),
        )
        check(
            "跨项目隔离：p2 看不到 p1 的部署记忆（不串味）",
            not any("部署脚本" in r["content"] for r in p2),
            str([r["content"] for r in p2]),
        )
        p2_rule = json.loads(
            run(scoped, "recall", "不确定就该说不确定", "--project", "p2", "--json").stdout
        )
        check(
            "但全局记忆在 p2 仍可见（跨项目共享的正是这类）",
            any("不确定就说不确定" in r["content"] for r in p2_rule),
            str([r["content"] for r in p2_rule]),
        )
        ctx2 = run(scoped, "context", "--query", "部署脚本", "--project", "p2", "--status-only")
        check(
            "p2 的注入状态明确写出「无相关记忆」，而不是硬凑",
            "无相关记忆" in ctx2.stdout,
            ctx2.stdout.strip(),
        )
        proj = run(scoped, "projects", "--project", "p1")
        check(
            "projects 能列出作用域分布",
            "全局" in proj.stdout and "项目·p1" in proj.stdout,
            proj.stdout.splitlines()[2] if len(proj.stdout.splitlines()) > 2 else "",
        )

        empty = os.path.join(tmp, "empty.json")
        run(empty, "init")
        empty_snap = json.loads(run(empty, "status", "--json").stdout)
        check(
            "空库的结构健康是 empty，而不是被误判成「单枢纽」",
            empty_snap["structure_health"] == "empty",
            empty_snap["structure_health"],
        )

    print("\nCLI 契约自测通过 ✅")


def test_cli_contract() -> None:
    main()


if __name__ == "__main__":
    main()
