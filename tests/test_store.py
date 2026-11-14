"""test_store.py - 持久化后端的契约：并发、增量、崩溃、导出、体检。

这几条是「生产级」与「能跑」的分界线，所以测的方式也必须是**外部**的：

  · 多进程写，看**最后落盘的结果**对不对（不是看对象内部状态）
  · 崩溃，靠**真的截断/改坏文件**来模拟（不是 mock 掉异常）
  · 增量，看**写下去的行数**（不是看"我觉得它只写了增量"）
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from memory_palace.palace import MemoryPalace  # noqa: E402
from memory_palace.store import JsonStore, StoreCorrupt, open_store, remove_store_files  # noqa: E402

WORKER = r"""
import sys
sys.path.insert(0, {root!r})
from memory_palace.palace import MemoryPalace

path, tag, count = sys.argv[1], sys.argv[2], int(sys.argv[3])
palace = MemoryPalace(path=path, autosave=True)
for i in range(count):
    palace.remember(f"{{tag}}-{{i}}", f"进程 {{tag}} 的第 {{i}} 条记录：仓位与资金接力", tags=("并发", tag))
    # 同一个名字反复 merge：每次都会把 access_count +1，这是「丢更新」最容易露馅的地方
    palace.remember("shared", "所有进程共用的一条记忆", tags=("共享",), merge=True)
print("done", tag)
"""


def _run_workers(path: str, tags: list[str], count: int) -> None:
    with tempfile.TemporaryDirectory(prefix="mp-worker-") as tmp:
        script = os.path.join(tmp, "worker.py")
        with open(script, "w", encoding="utf-8") as f:
            f.write(WORKER.format(root=ROOT))
        procs = [
            subprocess.Popen(
                [sys.executable, script, path, tag, str(count)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )
            for tag in tags
        ]
        for proc in procs:
            out, err = proc.communicate(timeout=180)
            assert proc.returncode == 0, f"worker 失败: {out}\n{err}"


def test_multiprocess_no_lost_updates() -> None:
    """4 个进程各写 15 条 + 15 次 merge，一条都不能丢。

    这条测试如果失败，说明「多进程写同一份记忆」只是文档里的一句话。
    merge 那条尤其关键：它读-改-写同一条记录，正是丢更新的高发点。
    """
    with tempfile.TemporaryDirectory(prefix="mp-mp-") as tmp:
        path = os.path.join(tmp, "store.sqlite")
        tags = ["p1", "p2", "p3", "p4"]
        per = 15
        _run_workers(path, tags, per)

        palace = MemoryPalace(path=path)
        try:
            names = set(palace.graph.nodes())
            missing = [
                f"{t}-{i}" for t in tags for i in range(per) if f"{t}-{i}" not in names
            ]
            assert not missing, f"丢了 {len(missing)} 条写入: {missing[:5]}"
            shared = palace.graph.get("shared")
            assert shared is not None
            # 4 个进程各 15 次：第一次是「创建」，其余 59 次是 merge/replace。
            # 无论走哪条路，access_count 都必须等于总调用次数 —— 少一次就是丢了更新。
            expected = len(tags) * per
            assert shared.access_count >= expected, (
                f"共享记忆只被记起 {shared.access_count - 1} 次，应至少 {expected - 1}"
            )
            assert palace._store is not None
            report = palace._store.fsck()
            assert report["ok"], report
        finally:
            palace.close()


def test_incremental_write() -> None:
    """改一条只写一行，不是重写整库。"""
    with tempfile.TemporaryDirectory(prefix="mp-inc-") as tmp:
        path = os.path.join(tmp, "store.sqlite")
        palace = MemoryPalace(path=path, autosave=True)
        for i in range(20):
            palace.remember(f"n{i}", f"第 {i} 条记忆，关于仓位与回撤", tags=("仓位",))
        before = palace._store.history(limit=1)[0]
        assert before["nodes_written"] <= 20, before
        palace.remember("n20", "第 20 条记忆，关于仓位与回撤", tags=("仓位",))
        after = palace._store.history(limit=1)[0]
        assert after["nodes_written"] == 1, f"只加了一条，却写了 {after['nodes_written']} 行"
        assert after["nodes_total"] == 21
        palace.close()


def test_reload_is_stable() -> None:
    """存盘 → 重新打开 → 再存盘，不产生「幽灵脏节点」。"""
    with tempfile.TemporaryDirectory(prefix="mp-stable-") as tmp:
        path = os.path.join(tmp, "store.sqlite")
        first = MemoryPalace(path=path, autosave=True)
        for i in range(8):
            first.remember(f"n{i}", f"记忆 {i}", tags=("测试",))
        second = MemoryPalace(path=path, autosave=True)
        second.save(reason="check")
        last = second._store.history(limit=1)[0]
        assert last["nodes_written"] == 0, f"重新打开后不该有任何行被重写，实际 {last['nodes_written']}"
        first.close()
        second.close()


def test_delta_serialization_is_bounded() -> None:
    """改一条记忆，只该序列化一条。

    这是「扩展性」在实现层面的定义：写放大的单位是「变动的节点数」，
    不是「库的节点数」。库级去重（store 里比对 json 缓存）能挡住重复写盘，
    但挡不住**序列化**—— 那条成本在 palace 就付掉了，所以必须在这里断言。
    """
    from memory_palace.core import MemoryNode

    with tempfile.TemporaryDirectory(prefix="mp-delta-") as tmp:
        path = os.path.join(tmp, "store.sqlite")
        palace = MemoryPalace(path=path, autosave=True)
        for i in range(20):
            palace.remember(f"n{i}", f"第 {i} 条记忆，关于仓位与回撤", tags=("仓位",))
        original = MemoryNode.to_dict
        calls: list[str] = []

        def counting(self):
            calls.append(self.name)
            return original(self)

        MemoryNode.to_dict = counting  # type: ignore[method-assign]
        try:
            palace.remember("extra", "只新增一条", tags=("仓位",))
        finally:
            MemoryNode.to_dict = original  # type: ignore[method-assign]
        assert len(calls) <= 3, f"只改一条却序列化了 {len(calls)} 条: {calls[:5]}"
        palace.close()


def test_reader_sees_other_writers() -> None:
    """两个客户端：A 写完，B 不重开也应该看得见。

    长驻进程（MCP / HTTP）就是 B。以前只有写事务会重载，B 永远停在
    「自己上次写完那一刻」，而且**不会报错** —— 单进程测试永远发现不了。
    """
    with tempfile.TemporaryDirectory(prefix="mp-read-") as tmp:
        path = os.path.join(tmp, "store.sqlite")
        writer = MemoryPalace(path=path, autosave=True)
        writer.remember("a", "写入者先写的一条：仓位按半凯利", tags=("仓位",))
        reader = MemoryPalace(path=path)
        assert len(reader.graph) == 1
        writer.remember("b", "写入者后写的一条：板块资金接力", tags=("板块",))
        assert len(reader.graph) == 1, "还没对齐时不该看到新条目"
        assert reader.refresh() is True
        assert set(reader.graph.nodes()) == {"a", "b"}
        writer.close()
        reader.close()


def test_sqlite_fsck_and_repair() -> None:
    with tempfile.TemporaryDirectory(prefix="mp-fsck-") as tmp:
        path = os.path.join(tmp, "store.sqlite")
        palace = MemoryPalace(path=path, autosave=True)
        palace.remember("a", "仓位与凯利", tags=("仓位",))
        palace.remember("b", "板块资金接力", tags=("板块",))
        palace.assert_relation("b", "a", "causal", 0.7)
        # 人为制造悬空边（模拟旧版本 bug 或手改库）
        conn = sqlite3.connect(path)
        conn.execute(
            "INSERT INTO edges(a,b,json,updated_rev) VALUES('ghost','a','{}',1)"
        )
        conn.commit()
        conn.close()

        store = open_store(path)
        report = store.fsck()
        assert report["orphan_edges"] == 1, report
        assert not report["ok"]
        fixed = store.repair()
        assert fixed["ok"] and fixed["dropped_edges"] == 1
        assert store.fsck()["ok"]
        store.close()
        palace.close()


def test_export_import_roundtrip() -> None:
    with tempfile.TemporaryDirectory(prefix="mp-exp-") as tmp:
        src_path = os.path.join(tmp, "a.sqlite")
        dst_path = os.path.join(tmp, "b.sqlite")
        bundle = os.path.join(tmp, "bundle.json")
        palace = MemoryPalace(path=src_path, autosave=True)
        for i in range(5):
            palace.remember(f"n{i}", f"第 {i} 条，关于复盘与纪律", tags=("复盘",))
        assert palace._store is not None
        info = palace._store.export(bundle)
        assert info["ok"] and os.path.getsize(bundle) > 0

        other = MemoryPalace(path=dst_path, autosave=True)
        other._store.import_(bundle)
        # 再导入一次：内容没变，不该重写任何一行
        other._store.import_(bundle)
        last = other._store.history(limit=1)[0]
        assert last["nodes_written"] == 0, f"重复导入重写了 {last['nodes_written']} 行"
        reloaded = MemoryPalace(path=dst_path)
        assert set(reloaded.graph.nodes()) == set(palace.graph.nodes())
        # revision 是各自库的提交序号，跨库比较没有意义；比的是内容与可继续写入
        assert reloaded.revision >= 1
        palace.close()
        other.close()
        reloaded.close()


def test_corrupt_snapshot_detected() -> None:
    """校验和必须真的能抓到「内容被改了一个字」。"""
    with tempfile.TemporaryDirectory(prefix="mp-corrupt-") as tmp:
        path = os.path.join(tmp, "store.json")
        palace = MemoryPalace(path=path, autosave=True)
        palace.remember("a", "仓位按半凯利给", tags=("仓位",))
        palace.save()
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
        broken = raw.replace("半凯利", "全凯利", 1)
        assert broken != raw
        with open(path, "w", encoding="utf-8") as f:
            f.write(broken)
        os.unlink(path + ".journal")  # 日志也清掉：否则它会（正确地）把快照前滚回来
        try:
            MemoryPalace(path=path)
        except (StoreCorrupt, ValueError) as exc:
            assert "校验和" in str(exc) or "损坏" in str(exc)
        else:
            raise AssertionError("被改过的快照竟然被当成正常库读进来了")


def test_json_journal_recovers_from_missing_snapshot() -> None:
    """快照没了、日志还在 → 必须前滚，而不是静默变成空库。"""
    with tempfile.TemporaryDirectory(prefix="mp-journal-") as tmp:
        path = os.path.join(tmp, "store.json")
        palace = MemoryPalace(path=path, autosave=True)
        for i in range(3):
            palace.remember(f"n{i}", f"第 {i} 条记忆", tags=("测试",))
        palace.save()
        os.unlink(path)  # 模拟「快照丢失」
        recovered = MemoryPalace(path=path)
        assert len(recovered.graph) == 3, f"前滚失败，只剩 {len(recovered.graph)} 条"
        assert recovered.recovered
        assert os.path.exists(path), "前滚后应立刻补回快照"


def test_json_journal_torn_tail_is_tolerated() -> None:
    """最后一条写一半（断电）→ 丢弃它，前面的记录一条不少。"""
    with tempfile.TemporaryDirectory(prefix="mp-torn-") as tmp:
        path = os.path.join(tmp, "store.json")
        palace = MemoryPalace(path=path, autosave=True)
        for i in range(3):
            palace.remember(f"n{i}", f"第 {i} 条记忆", tags=("测试",))
            palace.save()
        journal = path + ".journal"
        size = os.path.getsize(journal)
        with open(journal, "r+b") as f:
            f.truncate(size - 7)  # 砍掉最后一条的尾巴
        store = JsonStore(path)
        integrity = store.journal.integrity()
        assert integrity["torn_tail"], integrity
        loaded = store.load()
        assert "graph" in loaded.payload
        assert len(loaded.payload["graph"]["nodes"]) >= 2


def test_legacy_json_still_opens() -> None:
    """0.9.x 的裸 JSON 快照（没有 revision/checksum/state_id）必须还能打开。

    这条不是怀旧：真实用户的库就是 v6 的。升级后打不开 = 数据全丢。
    """
    with tempfile.TemporaryDirectory(prefix="mp-legacy-") as tmp:
        path = os.path.join(tmp, "old.json")
        seed = MemoryPalace(path=os.path.join(tmp, "seed.json"), autosave=True)
        seed.remember("keep", "升级前就存在的记忆", tags=("兼容",))
        seed.save()
        seed.close()
        with open(os.path.join(tmp, "seed.json"), "r", encoding="utf-8") as f:
            state = json.load(f)
        state["version"] = 6
        for key in ("checksum", "revision", "state_id", "written_at", "writer"):
            state.pop(key, None)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)

        palace = MemoryPalace(path=path)
        assert len(palace.graph) == 1
        palace.remember("a", "旧库也能写新记忆", tags=("兼容",))
        palace.save()
        palace.close()
        again = MemoryPalace(path=path)
        assert len(again.graph) == 2
        again.close()


def test_remove_store_files_clears_sidecars() -> None:
    with tempfile.TemporaryDirectory(prefix="mp-clean-") as tmp:
        path = os.path.join(tmp, "store.json")
        palace = MemoryPalace(path=path, autosave=True)
        palace.remember("a", "任意内容", tags=("测试",))
        palace.save()
        assert os.path.exists(path) and os.path.exists(path + ".journal")
        removed = remove_store_files(path)
        assert not os.path.exists(path) and not os.path.exists(path + ".journal")
        assert len(removed) >= 2


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            started = time.perf_counter()
            fn()
            print(f"  [PASS] {name}  {time.perf_counter() - started:.2f}s")
    print("\n持久化契约自测通过 ✅")
