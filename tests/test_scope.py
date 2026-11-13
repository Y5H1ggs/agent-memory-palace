"""test_scope.py - 跨项目记忆的断言：分层、隔离、提升、迁移。

    python tests/test_scope.py

这一组要防的不是"功能没做"，是**串味**：
  · 项目 A 的部署细节出现在项目 B 的注入块里（会被当成 B 的事实用）；
  · 用户偏好被锁在某个项目里（换个项目就要重说一遍）；
  · 两个项目的碎片被沉淀成一条"结论"（一条不属于任何项目的记忆）；
  · 旧快照升级后记忆全变成"某个项目的"（实际应该是全局的）。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory_palace import MemoryPalace, MutableClock
from memory_palace.cli import build_context
from memory_palace.scope import (
    GLOBAL,
    detect_project,
    normalize_project,
    parse_remote,
    scopes_for,
)

DAY = 86400.0
PASS = 0
FAIL = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {label}" + (f"  · {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {label}" + (f"  · {detail}" if detail else ""))


def test_project_id() -> None:
    print("\n项目标识：换目录、换机器都要认得同一个项目")
    cases = {
        "git@github.com:owner/repo.git": "owner/repo",
        "https://github.com/owner/repo.git": "owner/repo",
        "ssh://git@host:22/owner/repo": "owner/repo",
        "https://gitlab.com/group/sub/repo": "sub/repo",
    }
    ok = all(parse_remote(url) == want for url, want in cases.items())
    check("四种 remote 写法都归一成 owner/repo", ok,
          str({u: parse_remote(u) for u in cases}))

    check(
        "显式指定优先于一切（--project / 环境变量）",
        normalize_project("my-repo") == "proj:my-repo",
        normalize_project("my-repo"),
    )
    here = detect_project()
    check(
        "在仓库里打开时认得 git remote，而不是目录名",
        here.startswith("proj:") and "/" in here,
        here,
    )
    with tempfile.TemporaryDirectory(prefix="mp-plain-") as tmp:
        plain = detect_project(tmp)
    check(
        "没有 git 时退回「目录名+路径哈希」：可读且稳定",
        plain.startswith("proj:mp-plain-") and plain == detect_project(tmp),
        plain,
    )


def test_layers() -> None:
    print("\n分层：跨项目与项目内，默认是两者相加")
    check(
        "auto = 全局 + 当前项目",
        scopes_for("auto", "proj:a") == {GLOBAL, "proj:a"},
        str(scopes_for("auto", "proj:a")),
    )
    check("global 视图只有全局", scopes_for("global", "proj:a") == {"global"}, str(scopes_for("global", "proj:a")))
    check("project 视图只有当前项目", scopes_for("project", "proj:a") == {"proj:a"}, str(scopes_for("project", "proj:a")))
    check("all 不过滤（调试用）", scopes_for("all", "proj:a") is None, "None")

    clock = MutableClock(1_996_000_000.0)
    a = MemoryPalace(clock=clock, project="repo-a")
    a.remember("rule", "禁止绝对化措辞", tags=("硬规则",), tier="core", pinned=True, scope="global")
    a.remember("deploy-a", "A 项目的部署脚本要先 source env.sh", tags=("部署",))

    b = MemoryPalace(clock=clock, project="repo-b")
    b.graph = a.graph  # 共用同一张图：模拟同一个 store 被两个项目打开
    b.remember("build-b", "B 项目用 pnpm 而不是 npm", tags=("构建",))

    a_hits = [n.name for n in a.recall(query="部署脚本", scopes=scopes_for("auto", "repo-a"))]
    b_hits = [n.name for n in b.recall(query="部署脚本", scopes=scopes_for("auto", "repo-b"))]
    check(
        "项目 A 看得到自己的部署记忆",
        "deploy-a" in a_hits,
        str(a_hits),
    )
    check(
        "项目 B **看不到** A 的部署记忆（不串味）",
        "deploy-a" not in b_hits,
        str(b_hits),
    )
    # 单独查它，避免被"相对门槛"挡掉（高分的部署记忆会把低分规则压下去，那是检索排序，
    # 不是作用域问题 —— 断言要测的是后者）
    rule_a = [n.name for n in a.recall(query="禁止绝对化措辞", scopes=scopes_for("auto", "repo-a"))]
    rule_b = [n.name for n in b.recall(query="禁止绝对化措辞", scopes=scopes_for("auto", "repo-b"))]
    check(
        "全局的硬规则两个项目都看得到（跨项目共享）",
        "rule" in rule_a and "rule" in rule_b,
        f"A={rule_a} B={rule_b}",
    )
    check(
        "默认写入落在当前项目，不是全局",
        a.graph.get("deploy-a").scope == "proj:repo-a",
        a.graph.get("deploy-a").scope,
    )
    check(
        "scope_counts 能一眼看出分布",
        a.scope_counts() == {"proj:repo-a": 1, "proj:repo-b": 1, "global": 1},
        str(a.scope_counts()),
    )


def test_context_and_promote() -> None:
    print("\n注入与提升：显式决定，不自动泛化")
    clock = MutableClock(1_996_000_000.0)
    a = MemoryPalace(clock=clock, project="repo-a")
    a.remember("rule", "禁止绝对化措辞", tags=("硬规则",), tier="core", pinned=True, scope="global")
    a.remember("deploy-a", "A 项目部署脚本要先 source env.sh", tags=("部署",))
    b = MemoryPalace(clock=clock, project="repo-b")
    b.graph = a.graph

    ctx_b = build_context(b, query="部署脚本", budget=400)
    check(
        "B 的注入块里没有 A 的部署记忆，且明确弃答",
        all(i["name"] != "deploy-a" for i in ctx_b["items"]) and ctx_b["abstain"] is True,
        f"abstain={ctx_b['abstain']} items={[i['name'] for i in ctx_b['items']]}",
    )
    check(
        "但钉死的全局规则仍在（跨项目也要守规矩）",
        any(i.get("pinned") for i in ctx_b["items"]),
        str([(i["name"], i["pinned"]) for i in ctx_b["items"]]),
    )

    a.promote("deploy-a")
    ctx_b2 = build_context(b, query="部署脚本", budget=400)
    check(
        "promote 之后 B 才看得到（提升是一次显式决定）",
        any(i["name"] == "deploy-a" for i in ctx_b2["items"]) and ctx_b2["abstain"] is False,
        f"items={[i['name'] for i in ctx_b2['items']]}",
    )
    check(
        "提升不留旧值（scope 真的改了，不是复制一份）",
        a.graph.get("deploy-a").scope == "global" and a.scope_counts().get("proj:repo-a", 0) == 0,
        str(a.scope_counts()),
    )


def test_consolidation_isolation() -> None:
    print("\n沉淀不跨项目：否则会造出「不属于任何项目」的结论")
    from memory_palace.consolidation import ConsolidationEngine

    clock = MutableClock(1_996_000_000.0)
    palace = MemoryPalace(clock=clock, project="repo-a")
    engine = ConsolidationEngine(palace._config)
    for i, text in enumerate(
        ("部署前必须先跑迁移脚本", "部署顺序：迁移 → 构建 → 发布", "部署失败要回滚到上一版")
    ):
        palace.remember(f"a{i}", text, tags=("部署", "迁移"), scope="proj:repo-a")
        palace.remember(f"b{i}", text, tags=("部署", "迁移"), scope="proj:repo-b")

    candidates = [
        n for n in palace.graph.all_nodes()
        if n.tier in ("ephemeral", "event") and not n.is_retired()
    ]
    clusters = engine._clusters(candidates, palace.graph)
    mixed = [[m.name for m in c] for c in clusters if len({m.scope for m in c}) > 1]
    check(
        "没有任何簇跨项目（同内容的 A/B 碎片各聚各的）",
        not mixed,
        f"跨域簇 {mixed}",
    )
    check(
        "同项目内确实成簇了（否则上一条是空胜利）",
        any(len(c) >= 2 for c in clusters),
        str([[m.name for m in c] for c in clusters]),
    )

    result = engine.run(palace.graph, palace.now())
    mixed_created = [
        name for name in result.created
        if palace.graph.get(name)
        and len({palace.graph.get(m).scope for m in palace.graph.get(name).consolidated_from}) > 1
    ]
    check(
        "真实沉淀后，没有一条结论的碎片来自多个项目",
        not mixed_created,
        f"新建 {result.created} / 跨域 {mixed_created}",
    )
    scopes = {palace.graph.get(n).scope for n in result.created if palace.graph.get(n)}
    check(
        "沉淀出的结论继承碎片的作用域（不会飘成全局）",
        scopes <= {"proj:repo-a", "proj:repo-b"},
        str(scopes),
    )


def test_legacy_snapshot() -> None:
    print("\n旧快照迁移：老记忆应该被当成「全局」，不是某个项目的")
    clock = MutableClock(1_996_000_000.0)
    palace = MemoryPalace(clock=clock, project="repo-a")
    palace.remember("old", "升级前写的记忆", tags=("旧",))
    with tempfile.TemporaryDirectory(prefix="mp-legacy-") as tmp:
        path = os.path.join(tmp, "store.json")
        palace.save(path)
        # 模拟 v6 之前写的快照：节点里没有 scope 字段
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
        nodes = state["graph"]["nodes"]
        for node in (nodes.values() if isinstance(nodes, dict) else nodes):
            node.pop("scope", None)
        state["version"] = 5
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)

        reloaded = MemoryPalace(path=path, clock=clock, project="repo-b")
        node = reloaded.graph.get("old")
        check(
            "缺 scope 的旧节点被当成全局（换项目仍然记得）",
            node is not None and node.scope == "global",
            node.scope if node else "节点丢了",
        )
        check(
            "旧快照能正常加载（升级不失联）",
            len(reloaded.graph) == 1,
            f"nodes={len(reloaded.graph)}",
        )
        check(
            "从 B 项目也能检索到它（全局可见）",
            [n.name for n in reloaded.recall(query="升级前写的记忆")] == ["old"],
            str([n.name for n in reloaded.recall(query="升级前写的记忆")]),
        )


def main() -> int:
    test_project_id()
    test_layers()
    test_context_and_promote()
    test_consolidation_isolation()
    test_legacy_snapshot()
    total = PASS + FAIL
    print(f"\n{'=' * 60}\n{total} 项断言：{PASS} 通过 / {FAIL} 失败\n{'=' * 60}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
