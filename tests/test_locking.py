"""test_locking.py - 跨进程写锁：真的跨进程，不是「同一个对象加两次」。

用子进程测是必须的：`threading.RLock` 在单进程里能通过所有同类测试，
而这正是这个库 0.9.x 在网上被指出的问题 —— 测的是同一件事，却测不出真问题。
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from memory_palace.locking import FileLock, LockBusy, holder_of  # noqa: E402

CHILD = r"""
import sys, time
sys.path.insert(0, {root!r})
from memory_palace.locking import FileLock, LockBusy
path = {path!r}
try:
    lock = FileLock(path, timeout=float(sys.argv[1]))
    lock.acquire()
    print("acquired")
    lock.release()
except LockBusy as exc:
    print("busy", exc.holder.get("pid"))
"""


def test_cross_process_exclusion() -> None:
    with tempfile.TemporaryDirectory(prefix="mp-lock-") as tmp:
        path = os.path.join(tmp, "store.lock")
        script = os.path.join(tmp, "child.py")
        with open(script, "w", encoding="utf-8") as f:
            f.write(CHILD.format(root=ROOT, path=path))

        lock = FileLock(path, timeout=1.0, label="parent")
        with lock:
            info = holder_of(path)
            assert info["pid"] == os.getpid()
            assert info["label"] == "parent"
            proc = subprocess.run(
                [sys.executable, script, "0.3"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=60,
            )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.startswith("busy"), f"子进程竟然拿到了锁: {proc.stdout!r}"
        assert str(os.getpid()) in proc.stdout, "超时信息里必须报出是谁占着锁"

        # 释放之后子进程应该能立刻拿到
        proc = subprocess.run(
            [sys.executable, script, "1.0"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )
        assert proc.stdout.strip() == "acquired", proc.stdout


def test_reentrant_same_thread() -> None:
    with tempfile.TemporaryDirectory(prefix="mp-lock-") as tmp:
        path = os.path.join(tmp, "x.lock")
        outer = FileLock(path, timeout=1.0)
        with outer:
            with FileLock(path, timeout=0.1) as inner:
                assert inner.held
            assert outer.held
        assert not outer.held


def test_other_thread_blocks_until_release() -> None:
    with tempfile.TemporaryDirectory(prefix="mp-lock-") as tmp:
        path = os.path.join(tmp, "t.lock")
        order: list[str] = []
        first = FileLock(path, timeout=1.0)
        first.acquire()
        try:
            def worker() -> None:
                lock = FileLock(path, timeout=2.0)
                lock.acquire()
                order.append("second")
                lock.release()

            thread = threading.Thread(target=worker)
            thread.start()
            time.sleep(0.15)
            assert order == [], "持锁期间别的线程不该进得来"
        finally:
            first.release()
        thread.join(timeout=5)
        assert order == ["second"]


def test_timeout_is_total_not_per_try() -> None:
    """超时是「总等待时间」，不是「单次尝试时间」。

    必须在**另一个线程**里试：同一线程会走重入路径直接成功（这是设计），
    在那里测超时测的是别的东西。
    """
    with tempfile.TemporaryDirectory(prefix="mp-lock-") as tmp:
        path = os.path.join(tmp, "b.lock")
        holder = FileLock(path, timeout=1.0)
        holder.acquire()
        result: dict[str, float] = {}

        def worker() -> None:
            started = time.perf_counter()
            try:
                FileLock(path, timeout=0.2, poll=0.01).acquire()
            except LockBusy:
                result["elapsed"] = time.perf_counter() - started

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(timeout=5)
        try:
            assert "elapsed" in result, "另一个线程应该等到超时"
            assert 0.15 <= result["elapsed"] < 1.0, f"等待 {result['elapsed']:.2f}s 超出区间"
        finally:
            holder.release()


def test_stale_lock_metadata_reports_missing_pid() -> None:
    with tempfile.TemporaryDirectory(prefix="mp-lock-") as tmp:
        path = os.path.join(tmp, "s.lock")
        # 元信息在旁路文件里：锁文件本身只负责锁，不写内容
        with open(path + ".holder", "w", encoding="utf-8") as f:
            f.write('{"pid": 999999, "host": "x", "since": 0, "label": "ghost"}')
        info = holder_of(path)
        assert info.get("stale") is True


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  [PASS] {name}")
    print("\n跨进程锁自测通过 ✅")
