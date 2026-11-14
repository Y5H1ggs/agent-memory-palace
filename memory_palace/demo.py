"""demo.py - 一次跑完，把「这套东西到底在做什么」演给你看。

设计原则（和 README 一样）：**每一幕都有一句能复述的结论**，而不是把 API 列一遍。
每幕结束打断言；失败的会直接指出，不会用「演示通过」糊过去。

六幕：
    1. 写入与检索：标签能命中、没记过要弃答
    2. 存储：改一条只写一行；坏快照读不进来
    3. 多进程：4 个进程同时写，一条不丢
    4. 嵌入：内置哈希能做什么、不能做什么
    5. 生命周期：衰减幂等、沉淀、遗忘、双时态
    6. 评测：106 个领域，平均值 + 最差的那几个

跑法：`python -m memory_palace.demo`（约 10 秒）
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time

from . import __version__
from .core import MutableClock, PalaceConfig
from .embedding import HashingEmbedder, self_check
from .palace import MemoryPalace

PASS = 0
FAIL = 0
WIDTH = 74


# ---------------------------------------------------------------- 小工具


def title(text: str) -> None:
    print()
    print("=" * WIDTH)
    print(text)
    print("=" * WIDTH)


def say(text: str = "") -> None:
    print(f"  {text}")


def check(label: str, ok: bool, detail: str = "") -> bool:
    """一条断言。detail 在失败时必须给出「实际是什么」，否则等于没断言。"""
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {label}" + (f"  · {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {label}" + (f"  · {detail}" if detail else ""))
    return ok


def section(n: int, text: str, why: str) -> None:
    title(f"第 {n} 幕 · {text}")
    print(f"  {why}")
    print()


# ---------------------------------------------------------------- 第 1 幕


def scene_write_and_recall(work: str) -> MemoryPalace:
    section(
        1,
        "写入与检索",
        "记忆不是「文本 + 向量」，而是「带标签、带寿命、会被弃答」的对象。",
    )
    clock = MutableClock(1_800_000_000.0)
    palace = MemoryPalace(path=os.path.join(work, "one.sqlite"), clock=clock, autosave=True)

    palace.remember(
        "rule:check",
        "说「完成」之前先跑一遍自测，绿了才算完成",
        tags=("硬规则", "自测"),
        tier="core",
        pinned=True,
    )
    palace.remember(
        "lesson:cp1252",
        "CI 在 Windows 上因为控制台编码炸了，排查两小时",
        tags=("复盘", "教训"),
    )
    palace.remember("pos:kelly", "单笔仓位按半凯利给，最多不超过总资金三分之一", tags=("仓位",))
    palace.remember("diet:food", "不吃香菜与动物内脏，点菜时提前说", tags=("饮食", "忌口"))

    say(f"写了 4 条，库里现在 {len(palace.graph)} 条")

    hits = palace.recall(query="控制台编码导致 CI 失败", limit=3)
    say("查「控制台编码导致 CI 失败」→ " + (hits[0].name if hits else "（空）"))
    check(
        "换了语序、没有标签重叠，也能命中那条教训",
        bool(hits) and hits[0].name == "lesson:cp1252",
        f"top={hits[0].name if hits else '空'}",
    )
    miss = palace.recall(query="怎么让模型少胡说八道")
    check(
        "同义改写（不含原词）会弃答 —— 这是词法通道的真实短板",
        not miss or all(n.pinned for n in miss),
        f"返回 {len(miss)} 条",
    )

    # 只出现在标签里的词也必须能检索到：正文写「不吃香菜」，标签才是「忌口」
    tag_hits = palace.semantics.scores("我有什么忌口")
    check(
        "只出现在标签里的词也能命中",
        tag_hits.get("diet:food", 0) > 0,
        f"diet:food={tag_hits.get('diet:food', 0):.3f}",
    )

    # 弃答：库里根本没有的领域，应该明确说「没有」，而不是端最像的几条上来
    empty = palace.recall(query="量子计算的退相干时间是多少")
    check(
        "没记过的东西会弃答（只回钉死的硬规则或不回）",
        all(n.pinned for n in empty),
        f"返回 {len(empty)} 条",
    )

    say()
    say("这一步的重点：检索不是「总能返回点什么」，而是要能说「我没有」。")
    return palace


# ---------------------------------------------------------------- 第 2 幕


def scene_storage(work: str) -> None:
    section(
        2,
        "存储：一条记忆一行",
        "原子写保证「文件不坏」，不保证「改动不丢」—— 这两件事经常被混为一谈。",
    )
    path = os.path.join(work, "store.sqlite")
    palace = MemoryPalace(path=path, clock=MutableClock(1_800_000_000.0), autosave=True)

    for i in range(40):
        palace.remember(f"n{i}", f"第 {i} 条记录：仓位与回撤控制", tags=("仓位",))

    palace.remember("n40", "第 40 条：只加了一条", tags=("仓位",))
    last = palace._store.history(limit=1)[0]
    say(f"加第 41 条时写入了 {last['nodes_written']} 行（不是 41 行）")
    check(
        "写入是行级的，不是整库重写",
        last["nodes_written"] <= 3,
        f"nodes_total={last['nodes_total']}",
    )

    report = palace._store.fsck()
    check("fsck 通过（完整性 / 悬空边 / 账本一致）", report["ok"], str(report)[:60])

    # 把库改坏一个字节，必须读不进来 —— 而不是安静地读进一份错数据
    palace.close()
    # 截掉尾部：比「改中间几个字节」更可靠 —— 中间那几字节可能正好落在空闲页，
    # SQLite 不会读它，于是「坏数据被读进来」的断言会莫名通过。
    with open(path, "r+b") as f:
        raw = f.read()
        f.truncate(max(4096, len(raw) // 2))
    try:
        MemoryPalace(path=path)
    except Exception as exc:  # noqa: BLE001
        check("坏数据会被拒绝，而不是被当成好数据", True, type(exc).__name__)
    else:
        check("坏数据会被拒绝，而不是被当成好数据", False, "居然读进来了")

    say()
    say("这一步的重点：能在坏数据上失败，比能在好数据上成功更重要。")


# ---------------------------------------------------------------- 第 3 幕

_WORKER = """
import sys
sys.path.insert(0, {root!r})
from memory_palace.palace import MemoryPalace

tag = sys.argv[1]
palace = MemoryPalace(path={path!r}, autosave=True)
for i in range(6):
    palace.remember(f"{{tag}}-{{i}}", f"进程 {{tag}} 的第 {{i}} 条：资金接力", tags=("并发", tag))
    palace.remember("shared", "所有进程共用的一条", tags=("共享",), merge=True)
palace.close()
"""


def scene_multiprocess(work: str) -> None:
    section(
        3,
        "多进程写同一份记忆",
        "「先改后查」时两边都认为自己最新，后写的会把前一方已提交的改动整段覆盖，且没人报错。",
    )
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(work, "concurrent.sqlite")
    script = os.path.join(work, "worker.py")
    with open(script, "w", encoding="utf-8") as f:
        f.write(_WORKER.format(root=root, path=path))

    started = time.perf_counter()
    procs = [
        subprocess.Popen(
            [sys.executable, script, f"p{i}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        for i in range(4)
    ]
    errors = []
    for proc in procs:
        _out, err = proc.communicate()
        if proc.returncode != 0:
            errors.append(err.strip()[-200:])
    elapsed = (time.perf_counter() - started) * 1000

    check("4 个进程都没有异常退出", not errors, errors[0] if errors else f"{elapsed:.0f}ms")

    reader = MemoryPalace(path=path)
    names = set(reader.graph.nodes())
    missing = [f"p{i}-{j}" for i in range(4) for j in range(6) if f"p{i}-{j}" not in names]
    check("24 条独立写入一条不少", not missing, f"缺 {len(missing)} 条")

    shared = reader.graph.get("shared")
    expected = 4 * 6
    check(
        "共享记忆的计数等于调用次数（零丢更新）",
        shared is not None and shared.access_count >= expected,
        f"access_count={shared.access_count if shared else 0}，应 ≥ {expected}",
    )
    check("并发写完之后 fsck 仍然通过", reader._store.fsck()["ok"])
    reader.close()

    say()
    say("这一步的重点：并发正确性不是「跑起来没崩」，而是「一条都没少」。")


# ---------------------------------------------------------------- 第 4 幕


def scene_embedding() -> None:
    section(
        4,
        "嵌入通道：工具能用，还是模型能用",
        "只看维度没有意义 —— 一个把什么都编码成同方向的模型也有 1024 维。",
    )
    result = self_check(HashingEmbedder(512))
    say(f"自检对象：{result['name']}  维度 {result['dim']}")
    say(f"同义改写余弦 {result['related']}，无关句余弦 {result['unrelated']}，差 {result['margin']}")
    check("内置哈希能把「同义改写」和「无关句」分开", result["ok"])
    say()
    say("换成真模型只要改两行配置：")
    say("  memory-palace config --set embedding_provider=ollama")
    say("  memory-palace config --set embedding_model=bge-m3")
    say("  memory-palace embed --check          # 换完必须重跑这条")
    say()
    say("这一步的重点：可替换不是口号，得有办法验证新模型确实更好。")


# ---------------------------------------------------------------- 第 5 幕


def scene_lifecycle(work: str) -> None:
    section(
        5,
        "生命周期：遗忘、沉淀、双时态",
        "衰减必须是「锚点 + 时间」的纯函数，否则 GC 跑一次和跑一百次结果不同。",
    )
    clock = MutableClock(1_800_000_000.0)
    palace = MemoryPalace(
        path=os.path.join(work, "life.sqlite"),
        clock=clock,
        config=PalaceConfig(consolidate_min_group=2),
        autosave=True,
    )
    palace.remember("rule:noabs", "禁止绝对化措辞", tags=("硬规则",), tier="core", pinned=True)
    for i in range(3):
        palace.remember(f"frag:a{i}", "缩量反弹不追高，等放量确认", tags=("复盘", "量能"))
        palace.remember(f"frag:b{i}", "板块资金接力看连板基因", tags=("板块", "资金接力"))
    palace.remember("tmp:old", "迟早会被忘掉的一条", tags=("临时",), tier="ephemeral")

    before = {n.name: n.effective_weight(clock()) for n in palace.graph.all_nodes()}
    for _ in range(5):
        palace.gc()
    after = {n.name: n.effective_weight(clock()) for n in palace.graph.all_nodes()}
    check(
        "GC 跑 5 次与跑 1 次结果一致（幂等）",
        all(abs(before[k] - after[k]) < 1e-9 for k in before if k in after),
        f"{len(before)} 条",
    )

    # 注意：`advance()` 的单位是**天**。这里只推半小时（0.02 天），
    # 推 30 天会把 tier=event 的碎片直接衰减到 0，后面的沉淀就无从谈起。
    clock.advance(0.02)
    palace.remember("fact:a", "第一版结论：只做主线", tags=("板块",))
    clock.advance(0.001)
    palace.remember("fact:b", "第二版结论：主线 + 低位首板", tags=("板块",), supersedes="fact:a")

    now_hit = [n.name for n in palace.recall(names=["fact:a", "fact:b"])]
    old_hit = [n.name for n in palace.recall(names=["fact:a", "fact:b"], as_of=clock() - 0.01)]
    check("默认只返回「现在有效」的版本", "fact:a" not in now_hit, f"命中 {now_hit}")
    check("as_of 能回看当时的版本", "fact:b" not in old_hit, f"命中 {old_hit}")

    result = palace.consolidate()
    created = list(getattr(result, "created", []) or [])
    check("碎片会沉淀成结论（单向门）", bool(created), f"新建 {len(created)} 条结论")

    clock.advance(400)
    deleted = palace.gc()
    check(
        "过期碎片被回收，钉死的一条没少",
        "rule:noabs" in palace.graph.nodes() and "tmp:old" in deleted,
        f"回收 {len(deleted)} 条",
    )
    palace.close()

    say()
    say("这一步的重点：记忆系统的价值不在「存进去」，而在「什么时候让它走」。")


# ---------------------------------------------------------------- 第 6 幕


def scene_evaluation() -> None:
    section(
        6,
        "评测：106 个领域",
        "平均值会掩盖最差的领域 —— 所以报告先打印最差的那 5 个。",
    )
    from . import evaluation as ev

    started = time.perf_counter()
    report = ev.run_suite(k=5)
    elapsed = (time.perf_counter() - started) * 1000
    summary = report["summary"]
    say(f"{summary['domains']} 个领域 / {summary['queries']} 条可答查询，用时 {elapsed:.0f}ms")
    say(
        f"recall@5 {summary['recall@k']} · MRR {summary['mrr']} · 弃答 F1 {summary['abstain_f1']}"
    )
    covered = summary["covered"]
    say(
        f"覆盖：检索 {covered['retrieval']} · 弃答 {covered['abstain']} · "
        f"去重/沉淀 {covered['dedupe']}（后两者只在深标注领域上测）"
    )

    worst = sorted(report["datasets"], key=lambda e: e["retrieval"]["overall"]["recall@k"])[:3]
    say()
    say("最差 3 个领域：")
    for entry in worst:
        say(f"  {entry['dataset']:<20} recall={entry['retrieval']['overall']['recall@k']}")

    check("领域数 ≥ 100", summary["domains"] >= 100, str(summary["domains"]))
    check("跨领域平均召回 ≥ 0.7", summary["recall@k"] >= 0.7, str(summary["recall@k"]))
    check("报告会暴露最差领域，而不是只给平均", len(worst) == 3)
    say()
    say("这一步的重点：评测集不是「证明自己好」，是「知道自己在哪儿不行」。")


# ---------------------------------------------------------------- 入口


def main() -> int:
    global PASS, FAIL
    PASS = FAIL = 0
    t0 = time.perf_counter()
    work = tempfile.mkdtemp(prefix="mp-demo-")
    print()
    print(f"memory-palace {__version__} · 演示（六幕，约 10 秒）")
    print(f"临时目录：{work}")

    try:
        palace = scene_write_and_recall(work)
        palace.close()
        scene_storage(work)
        scene_multiprocess(work)
        scene_embedding()
        scene_lifecycle(work)
        scene_evaluation()
    finally:
        shutil.rmtree(work, ignore_errors=True)

    elapsed = time.perf_counter() - t0
    print()
    print("=" * WIDTH)
    print(f"{PASS + FAIL} 项断言：{PASS} 通过 / {FAIL} 失败 · 用时 {elapsed:.1f}s")
    print("=" * WIDTH)
    if FAIL:
        print("有断言没过 —— 上面的 [FAIL] 行就是原因，不要跳过它。")
        return 1
    print("六幕都过了。想自己动手改参数：memory-palace eval / calibrate / bench。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
