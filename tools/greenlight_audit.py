"""greenlight_audit.py - 绿灯效应体检：把实现故意弄坏，看测试会不会变红。

    python tools/greenlight_audit.py            # 全跑
    python tools/greenlight_audit.py --list     # 只看变异清单
    python tools/greenlight_audit.py --only idf-flat

为什么需要这个工具：断言全绿只证明「我写的断言在本实现下成立」。
它不证明「换个实现会怎样」—— 一个写错的断言（比如断言恒真、断言空集合、
断言在空输入上通过）在正常实现下也是绿的，但它其实什么也没守。

变异测试是这类幻觉的唯一解药：**故意把实现改坏，红不了的断言就是没在守东西。**

判定很朴素：
  · 变异后测试变红 → 这条行为有人守着（好）
  · 变异后仍然全绿 → **绿灯漏洞**（这条行为没有任何断言能发现它坏了）

每个变异都必须写明「改坏了什么」与「该由哪些断言发现」。
写不出对应断言的变异，本身就是一份待办清单。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Mutant:
    mid: str
    path: str
    find: str
    replace: str
    why: str
    suites: tuple[str, ...]


MUTANTS: tuple[Mutant, ...] = (
    Mutant(
        "decay-flat",
        "memory_palace/core.py",
        "return self.base_weight * math.exp(-days / self.stability(now, affect_gain))",
        "return self.base_weight",
        "去掉指数衰减（强度永远等于锚点）",
        ("memory_palace.demo",),
    ),
    Mutant(
        "abstain-off",
        "memory_palace/cli.py",
        "abstain = bool(query) and not names and (weak or not relevant)",
        "abstain = False",
        "永远不弃答（无关查询也硬凑）",
        ("tests/test_temporal.py", "tests/test_cli.py"),
    ),
    Mutant(
        "idf-flat",
        "memory_palace/lexicon.py",
        "value = math.log((self.n + 1) / (df + 1)) + 1.0",
        "value = 1.0",
        "IDF 退化成全同权（高频词不再降权）",
        ("tests/test_lexicon.py",),
    ),
    Mutant(
        "negation-drop",
        "memory_palace/lexicon.py",
        "            if ch in NEGATIONS:\n                out[ch] += 1",
        "            if False:\n                out[ch] += 1",
        "丢掉否定词（「不追高」变「追高」）",
        ("tests/test_lexicon.py",),
    ),
    Mutant(
        "style-no-suppress",
        "memory_palace/style.py",
        "        if rule.suppressed_by is not None:\n            return False",
        "        if False:\n            return False",
        "裁决后输家仍生效（对立风格同时进 prompt）",
        ("memory_palace.demo",),
    ),
    Mutant(
        "scope-raw-name",
        "memory_palace/scope.py",
        "    here = normalize_project(project)",
        "    here = project",
        "不归一项目名（裸名与 proj: 前缀不相等 → 过滤静默失效）",
        ("tests/test_scope.py",),
    ),
    Mutant(
        "temporal-valid-always",
        "memory_palace/core.py",
        "return self.valid_to is None or moment < self.valid_to",
        "return True",
        "失效事实仍然可检索（双时态失效）",
        ("tests/test_temporal.py",),
    ),
    Mutant(
        "dedupe-off",
        "memory_palace/palace.py",
        "            if merge:\n                twin = self._find_duplicate(content, now)",
        "            if False:\n                twin = self._find_duplicate(content, now)",
        "关闭写入去重（同一句话攒成多条）",
        ("tests/test_temporal.py",),
    ),
    Mutant(
        "withdrawal-is-consent",
        "memory_palace/pragmatics.py",
        "return self.act not in _NOT_A_PREFERENCE and self.confidence >= 0.5",
        "return True",
        "把「随便你」当成「同意」（撤回≠同意失效）",
        ("tests/test_pragmatics.py",),
    ),
    Mutant(
        "hedge-ignored",
        "memory_palace/pragmatics.py",
        "        if self.hedged and tier == \"fact\":\n            tier = \"event\"",
        "        if False:\n            tier = \"event\"",
        "模糊限定不再降级（「可能不要」也进 fact）",
        ("tests/test_pragmatics.py",),
    ),
    Mutant(
        "consolidate-cross-scope",
        "memory_palace/consolidation.py",
        "            if a.scope != b.scope:\n                continue",
        "            if False:\n                continue",
        "跨项目聚类（A 的碎片与 B 的碎片结晶成同一条结论）",
        ("tests/test_scope.py",),
    ),
    Mutant(
        "speech-force-kept",
        "memory_palace/palace.py",
        "                    if twin.speech.force < speech.force:",
        "                    if False:",
        "合并时保留更软的语用档案（禁令被陈述吞掉）",
        ("tests/test_pragmatics.py",),
    ),
)


def run_suite(name: str, timeout: int = 180) -> tuple[bool, str]:
    """跑一个测试入口。返回 (是否通过, 摘要)。

    用 -m 跑模块，用路径跑脚本 —— 两种入口在仓库里都有，工具都要支持。
    """
    if name.startswith("tests/"):
        cmd = [sys.executable, str(ROOT / name)]
    else:
        cmd = [sys.executable, "-m", name]
    try:
        proc = subprocess.run(
            cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return False, "超时"
    out = (proc.stdout or "") + (proc.stderr or "")
    tail = ""
    for line in out.splitlines():
        if "[FAIL]" in line or "Error" in line:
            tail = line.strip()[:90]
            break
    return proc.returncode == 0, tail or (out.strip().splitlines()[-1][:90] if out.strip() else "无输出")


def apply_mutant(mutant: Mutant) -> str:
    path = ROOT / mutant.path
    original = path.read_text(encoding="utf-8")
    if mutant.find not in original:
        raise LookupError(
            f"{mutant.mid}: 找不到待替换片段（实现变了？）\n---\n{mutant.find}\n---"
        )
    path.write_text(original.replace(mutant.find, mutant.replace, 1), encoding="utf-8")
    return original


def restore(mutant: Mutant, original: str) -> None:
    (ROOT / mutant.path).write_text(original, encoding="utf-8")


def audit(only: str | None = None) -> int:
    selected = [m for m in MUTANTS if only is None or m.mid == only]
    if not selected:
        print(f"没有匹配的变异: {only}")
        return 2

    print(f"绿灯审计：{len(selected)} 个变异\n" + "=" * 78)
    escaped: list[Mutant] = []
    broken: list[str] = []
    start = time.time()

    for mutant in selected:
        try:
            original = apply_mutant(mutant)
        except LookupError as exc:
            print(f"  [SKIP] {mutant.mid:<24} {exc.args[0].splitlines()[0]}")
            broken.append(mutant.mid)
            continue
        try:
            green, detail = True, ""
            for suite in mutant.suites:
                ok, tail = run_suite(suite)
                if not ok:
                    green, detail = False, f"{Path(suite).name}: {tail}"
                    break
        finally:
            restore(mutant, original)

        if green:
            escaped.append(mutant)
            print(f"  [漏洞] {mutant.mid:<24} 改坏后仍然全绿 —— {mutant.why}")
        else:
            print(f"  [守住] {mutant.mid:<24} {detail}")

    elapsed = time.time() - start
    print("=" * 78)
    caught = len(selected) - len(escaped) - len(broken)
    print(
        f"结果：{caught}/{len(selected)} 个变异被抓到"
        f"（漏洞 {len(escaped)}，跳过 {len(broken)}），耗时 {elapsed:.1f}s"
    )
    if escaped:
        print("\n绿灯漏洞（这些行为目前没有任何断言能发现它坏了）：")
        for mutant in escaped:
            print(f"  · {mutant.mid}：{mutant.why}")
        print("\n补法：针对上面每一条写一条会变红的断言，然后重跑本工具。")
    if broken:
        print("\n跳过的变异（替换点已失效，需要跟着实现更新）：")
        for mid in broken:
            print(f"  · {mid}")
    return 0 if not escaped and not broken else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="绿灯效应体检（变异测试）")
    parser.add_argument("--list", action="store_true", help="只打印变异清单")
    parser.add_argument("--only", help="只跑某个变异 id")
    ns = parser.parse_args()
    if ns.list:
        for mutant in MUTANTS:
            print(f"{mutant.mid:<26} {mutant.why}")
            print(f"{'':<26} 文件 {mutant.path} · 测试 {', '.join(mutant.suites)}")
        return 0
    return audit(ns.only)


if __name__ == "__main__":
    sys.exit(main())
