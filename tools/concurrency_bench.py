"""concurrency_bench.py - 多进程同写一份记忆，到底能扛几个写者。

    python tools/concurrency_bench.py                     # 1/2/4/8 进程 × FULL/NORMAL
    python tools/concurrency_bench.py --procs 1,4 --writes 30 --sync NORMAL

为什么专门写这个：README 说过「SQLite 是单写者，写热点会排队」。那是一句
**定性**的话，而「排队」可以是 200ms，也可以是 20 秒 —— 差别决定它能不能
被用在多 agent 场景。没有曲线的定性结论，和广告词没有区别。

测的是**每次 remember 都落盘**（autosave=True）的最坏情况：
如果等攒批再写，数字会好看得多，但那不是 agent 每一步都调用的真实用法。

输出的三列各有含义：
  · 吞吐 = 所有人写完的总条数 / 墙钟。它反映「排队有没有把并行抵消掉」。
  · p50  = 单条写入的中位耗时。agent 感知的延迟。
  · p95  = 尾部。它才是「偶发卡顿」的来源，均值会把它藏起来。
"""
from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import shutil
import statistics
import sys
import tempfile
import time
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _worker(job: tuple[str, str, int, str]) -> dict[str, Any]:
    """一个写者：连续 remember N 条，每条都触发落盘。

    必须是模块级函数 —— Windows 用 spawn，闭包和 lambda 都传不过去。
    """
    path, sync, n, tag = job
    from memory_palace.core import MutableClock, PalaceConfig
    from memory_palace.palace import MemoryPalace

    cfg = PalaceConfig(store_synchronous=sync, lock_timeout=20.0)
    lat: list[float] = []
    errors: list[str] = []
    try:
        palace = MemoryPalace(path=path, clock=MutableClock(), autosave=True, config=cfg)
    except Exception as exc:  # 建库都可能失败，要如实记下来
        return {"tag": tag, "ok": 0, "lat": [], "errors": [f"open: {exc}"]}
    for i in range(n):
        started = time.perf_counter()
        try:
            palace.remember(
                f"{tag}-{i:04d}",
                f"并发压测 {tag} 第 {i} 条：仓位按半凯利，最多三只分散",
                tags=("压测", "并发"),
            )
        except Exception as exc:
            errors.append(f"write#{i}: {exc}")
            continue
        lat.append((time.perf_counter() - started) * 1000)
    try:
        palace.close()
    except Exception:
        pass
    return {"tag": tag, "ok": len(lat), "lat": lat, "errors": errors[:3]}


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[idx]


def run_case(procs: int, writes: int, sync: str, ctx, seed: int = 0) -> dict[str, Any]:
    tmp = tempfile.mkdtemp(prefix="mp-conc-")
    path = os.path.join(tmp, "store.sqlite")
    if seed:
        # 先灌一个「已经有存量」的库：真实场景里没人从空库开始并发。
        # 空库下每次写几乎只碰一页，测出来的曲线会偏乐观。
        from memory_palace.core import MutableClock
        from memory_palace.palace import MemoryPalace

        warm = MemoryPalace(path=path, clock=MutableClock())
        for i in range(seed):
            warm.remember(f"seed-{i:05d}", f"存量记忆 {i}：复盘与仓位口径", tags=("存量",))
        warm.close()
    jobs = [(path, sync, writes, f"p{i}") for i in range(procs)]
    started = time.perf_counter()
    try:
        with ctx.Pool(processes=procs) as pool:
            results = pool.map(_worker, jobs)
    finally:
        elapsed = time.perf_counter() - started
    lat: list[float] = []
    errors: list[str] = []
    ok = 0
    for item in results:
        lat.extend(item["lat"])
        ok += item["ok"]
        errors.extend(item["errors"])
    total = procs * writes
    row = {
        "procs": procs,
        "writes": writes,
        "seed": seed,
        "sync": sync,
        "ok": ok,
        "total": total,
        "fail": total - ok,
        "wall_ms": round(elapsed * 1000, 1),
        "throughput": round(ok / elapsed, 1) if elapsed > 0 else 0.0,
        "p50": round(statistics.median(lat), 2) if lat else 0.0,
        "p95": round(_pct(lat, 0.95), 2),
        "max": round(max(lat), 2) if lat else 0.0,
        "errors": errors[:3],
        "db_kb": round(os.path.getsize(path) / 1024, 1) if os.path.exists(path) else 0.0,
    }
    shutil.rmtree(tmp, ignore_errors=True)
    return row


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="concurrency-bench", description="多进程写并发压测")
    parser.add_argument("--procs", default="1,2,4,8", help="逗号分隔的进程数")
    parser.add_argument("--writes", type=int, default=40, help="每个进程写多少条")
    parser.add_argument("--sync", default="FULL,NORMAL", help="逗号分隔的 synchronous")
    parser.add_argument("--seed", type=int, default=0, help="先灌多少条存量记忆再并发写")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    procs_list = [int(x) for x in str(args.procs).split(",") if x.strip()]
    sync_list = [x.strip().upper() for x in str(args.sync).split(",") if x.strip()]
    ctx = mp.get_context("spawn")

    rows: list[dict[str, Any]] = []
    for sync in sync_list:
        for procs in procs_list:
            row = run_case(procs, args.writes, sync, ctx, seed=args.seed)
            rows.append(row)
            if not args.json:
                print(
                    f"synchronous={sync:<6} procs={procs:<2} seed={args.seed:<4} 写 {row['ok']:>4}/{row['total']:<4}"
                    f" 墙钟 {row['wall_ms']/1000:>5.2f}s  吞吐 {row['throughput']:>6.1f}/s"
                    f"  p50 {row['p50']:>6.2f}ms  p95 {row['p95']:>7.2f}ms"
                    f"  max {row['max']:>8.1f}ms"
                    + (f"  失败 {row['fail']}" if row["fail"] else "")
                )
                for err in row["errors"]:
                    print(f"    ! {err}")
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
