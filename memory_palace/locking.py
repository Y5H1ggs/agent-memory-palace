"""locking.py - 跨进程写锁：让「同一份记忆被多个进程写」从不可能变成可证明。

以前这个库只有 `threading.RLock`。它在**一个进程内**是对的，
但两个 `memory-palace` 进程（比如 Claude Code 与 DSH 同时挂同一个 store，
或者 cron 在跑 cycle 而你在 CLI 里 remember）会各自持有一把互不相识的锁：

    P1 读 → P1 改 → P2 读 → P2 改 → P1 写 → P2 写   # P1 的那次改动凭空消失

原子写保证的是「文件不会写坏」，不是「改动不会丢」。这两件事经常被混为一谈。
所以这里补的是**跨进程互斥**：操作系统级的字节范围锁，
不依赖任何第三方包（老规矩：零依赖）。

三种实现，按平台自动选，能力递减但语义一致：
  · POSIX   fcntl.flock(LOCK_EX | LOCK_NB)
  · Windows msvcrt.locking(LK_NBLCK)   —— 锁住文件第 0 个字节
  · 兜底    O_CREAT|O_EXCL 哨兵文件 + 陈旧锁清理（NFS 之类 flock 不可靠的场合）

可重入是必须的：palace 的一条写路径里 `remember()` 可能调到 `_maybe_save()`，
外面还套着一层 `with self._lock`。若锁不可重入，第二次 acquire 会等自己放到超时，
表现为「偶发卡住十秒」，极难排查。所以同进程同线程重入只加计数，不再碰 OS。
"""
from __future__ import annotations

import errno
import json
import os
import socket
import threading
import time
from typing import Any

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

try:  # Windows
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None  # type: ignore[assignment]

DEFAULT_TIMEOUT = 10.0
DEFAULT_POLL = 0.02
# 兜底哨兵锁的陈旧判定：持有者进程已死，或锁文件比这个时间还老。
STALE_LOCK_SECONDS = 120.0

_BUSY_ERRNOS = {errno.EACCES, errno.EAGAIN, errno.EDEADLK, errno.EWOULDBLOCK}


class LockBusy(TimeoutError):
    """等锁超时。带上是**谁**占着，否则调用方只能看到一个光秃秃的 TimeoutError。"""

    def __init__(self, path: str, waited: float, holder: dict[str, Any] | None = None):
        self.path = path
        self.waited = waited
        self.holder = holder or {}
        who = ""
        if self.holder:
            who = (
                f"，持有者 pid={self.holder.get('pid')} host={self.holder.get('host')}"
                f" 自 {self.holder.get('since')}s 起，操作={self.holder.get('label')!r}"
            )
        super().__init__(f"等待写锁超时（{waited:.1f}s）: {path}{who}")


class _Slot:
    """一个路径在**本进程内**的共享槽位：互斥量 + OS 句柄 + 重入深度。"""

    __slots__ = ("mutex", "fd", "depth", "pid")

    def __init__(self) -> None:
        self.mutex = threading.RLock()
        self.fd: int | None = None
        self.depth = 0
        self.pid = os.getpid()


_SLOTS: dict[str, _Slot] = {}
_SLOTS_GUARD = threading.Lock()


def _key(path: str) -> str:
    # normcase 让 Windows 上的 C:\a\b 与 c:/A/B 归一到同一把锁
    return os.path.normcase(os.path.abspath(path))


def _slot_for(key: str) -> _Slot:
    with _SLOTS_GUARD:
        slot = _SLOTS.get(key)
        if slot is None:
            slot = _SLOTS[key] = _Slot()
        return slot


def _os_lock(fd: int) -> None:
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return
    if msvcrt is not None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        return
    raise RuntimeError("当前平台既无 fcntl 也无 msvcrt，请用 FileLock(fallback=True)")


def _os_unlock(fd: int) -> None:
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return
    if msvcrt is not None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        # Windows 上没有 os.kill(pid, 0) 的可靠语义；用 ctypes 查进程句柄。
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return False
            kernel32.CloseHandle(handle)
            return True
        except Exception:  # pragma: no cover - 查不到就按「活」处理，避免误删别人的锁
            return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def holder_path(path: str) -> str:
    """持有者信息写在**旁路文件**里，不写进锁文件本身。

    Windows 的字节范围锁会让被锁字节在别的句柄下不可读写，
    于是「把元信息写进锁文件」在持锁期间根本做不到 ——
    元信息会静默丢失，而它恰恰是诊断卡死时唯一有用的东西。
    锁文件只负责锁，元信息另起一个文件，两边互不干涉。
    """
    return path + ".holder"


def write_holder(path: str, label: str = "", fd: int | None = None) -> None:
    """把持有者信息写进旁路文件。

    诊断价值：CLI 卡住时用户第一句话是「谁锁的」。没有这个字段，
    答案是「不知道」，只能让他去猜；有了它，`memory-palace lock` 直接报出 pid 与操作。
    """
    payload = json.dumps(
        {
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "since": round(time.time(), 3),
            "label": label,
        },
        ensure_ascii=False,
    )
    target = holder_path(path)
    try:
        with open(target, "w", encoding="utf-8") as f:
            f.write(payload)
    except OSError:
        pass  # 元信息写不进去不影响互斥本身：它是诊断信息，不是锁


def read_holder(path: str) -> dict[str, Any]:
    try:
        with open(holder_path(path), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


class FileLock:
    """跨进程 + 跨线程的排他锁，可重入。

    参数 `timeout` 是**总等待时间**，不是单次尝试；轮询间隔默认 20ms，
    比忙等省 CPU，比一次性阻塞好在超时可控（生产环境宁可报错，也不要无限挂住）。
    """

    def __init__(
        self,
        path: str,
        timeout: float = DEFAULT_TIMEOUT,
        poll: float = DEFAULT_POLL,
        label: str = "",
        fallback: bool = False,
    ) -> None:
        self.path = os.path.abspath(path)
        self.timeout = max(0.0, float(timeout))
        self.poll = max(0.001, float(poll))
        self.label = label
        self.fallback = fallback
        self._key = _key(self.path)
        self._slot: _Slot | None = None
        self._fallback_owned = False

    # ---- 采集 ----

    def _acquire_fallback(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o644)
                os.close(fd)
                self._fallback_owned = True
                return
            except FileExistsError:
                self._break_if_stale()
                if time.monotonic() >= deadline:
                    raise LockBusy(self.path, timeout, read_holder(self.path)) from None
                time.sleep(self.poll)

    def _break_if_stale(self) -> bool:
        holder = read_holder(self.path)
        pid = int(holder.get("pid") or 0)
        age = time.time() - float(holder.get("since") or 0.0)
        stale = (not pid or not _pid_alive(pid)) or age > STALE_LOCK_SECONDS
        if not stale:
            return False
        try:
            os.unlink(self.path)
            return True
        except OSError:
            return False

    def acquire(self, timeout: float | None = None) -> "FileLock":
        wait = self.timeout if timeout is None else max(0.0, float(timeout))
        deadline = time.monotonic() + wait
        slot = _slot_for(self._key)
        if not slot.mutex.acquire(timeout=wait):
            raise LockBusy(self.path, wait, read_holder(self.path))
        try:
            if slot.depth == 0:
                if self.fallback:
                    self._acquire_fallback(max(0.0, deadline - time.monotonic()))
                else:
                    fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
                    try:
                        while True:
                            try:
                                _os_lock(fd)
                                break
                            except OSError as exc:
                                if exc.errno not in _BUSY_ERRNOS:
                                    raise
                                if time.monotonic() >= deadline:
                                    raise LockBusy(
                                        self.path, wait, read_holder(self.path)
                                    ) from None
                                time.sleep(self.poll)
                    except BaseException:
                        os.close(fd)
                        raise
                    slot.fd = fd
                    write_holder(self.path, self.label, fd=fd)
            slot.depth += 1
            self._slot = slot
        except BaseException:
            slot.mutex.release()
            raise
        return self

    def release(self) -> None:
        slot = self._slot
        if slot is None:
            return
        self._slot = None
        slot.depth -= 1
        if slot.depth <= 0:
            slot.depth = 0
            try:
                if self._fallback_owned:
                    try:
                        os.unlink(self.path)
                    except OSError:
                        pass
                    self._fallback_owned = False
                elif slot.fd is not None:
                    try:
                        _os_unlock(slot.fd)
                    finally:
                        os.close(slot.fd)
                        slot.fd = None
            finally:
                slot.mutex.release()
        else:
            slot.mutex.release()

    # ---- 协议 ----

    def __enter__(self) -> "FileLock":
        return self.acquire()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()

    @property
    def held(self) -> bool:
        slot = self._slot
        return slot is not None and slot.depth > 0 and slot.pid == os.getpid()

    def __repr__(self) -> str:
        return f"FileLock({self.path!r}, held={self.held})"


def holder_of(path: str) -> dict[str, Any]:
    """诊断入口：这个 store 现在被谁锁着？没锁就返回空字典。"""
    info = read_holder(path)
    if not info:
        return {}
    if not _pid_alive(int(info.get("pid") or 0)):
        info["stale"] = True
    return info
