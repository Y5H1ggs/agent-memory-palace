"""store.py - 持久化门面：SQLite（默认）与 JSON（可移植/旧库）两个后端。

为什么默认换成 SQLite：JSON 快照的写入是「整份重写」——
1000 条记忆 1.3MB，每次 `remember` 都要重新序列化并覆盖整份文件。
在单进程里这只是慢；在多进程里它是个无法绕开的结构问题：

  · 两个人同时写，最后一个覆盖前一个（原子写保证文件不坏，不保证改动不丢）
  · 每次写都要重写全部内容，写放大与库的大小成正比
  · 想查「上周三那次修改之前是什么样」，只能靠额外的日志文件

SQLite 的 WAL 模式把这三件事一起解决：单写多读、行级锁、事务提交、
崩溃后自动前滚，且是 Python 标准库自带的（零依赖这条底线没有破）。

两个后端对外是同一个门面（load / save / begin / commit / rollback / fsck /
export / import），上层 `MemoryPalace` 不需要知道底下是谁：

    open_store("mem.sqlite")   →  SqliteStore   # 生产默认
    open_store("mem.json")     →  JsonStore     # 单文件可移植 / 旧库

JSON 后端保留是有意的：它的一份文件就是一个完整快照，适合放进 git、发给别人、
或者作为长期冷备。生产环境请用 SQLite。
"""
from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Protocol, runtime_checkable

from .core import atomic_write_text, read_json
from .journal import Journal
from .locking import FileLock, holder_of

SNAPSHOT_SCHEMA = 7


class StoreError(RuntimeError):
    pass


class StoreCorrupt(StoreError):
    """快照/日志都不可用。**不要**在这种情况下去初始化一个空库。"""

    def __init__(self, path: str, detail: str):
        self.path = path
        self.detail = detail
        super().__init__(f"记忆库损坏: {path}: {detail}")


class StoreEmpty(StoreError):
    """库文件在、但还没有任何提交（另一个进程刚建好、还没写第一笔）。

    这和「损坏」必须分开：损坏要报错，空库只是「还没有记忆」。
    把空库判成损坏，会让并发启动的第二个进程直接崩 —— 一个纯粹的启动时序问题
    被放大成数据事故。
    """


def _sha256(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


@dataclass
class SaveResult:
    ok: bool
    durable: bool
    revision: int
    bytes: int
    elapsed_ms: float
    journal_seq: int | None = None
    state_id: str = ""
    nodes_written: int = 0
    rebased: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "durable": self.durable,
            "revision": self.revision,
            "bytes": self.bytes,
            "elapsed_ms": round(self.elapsed_ms, 2),
            "journal_seq": self.journal_seq,
            "state_id": self.state_id,
            "nodes_written": self.nodes_written,
            "rebased": self.rebased,
        }


@dataclass
class LoadResult:
    payload: dict[str, Any]
    revision: int
    state_id: str
    source: str = "snapshot"  # snapshot / journal / sqlite
    recovered: bool = False
    note: str = ""


@runtime_checkable
class Store(Protocol):
    """两个后端共同遵守的契约。加一个后端，只要满足这 8 个方法。"""

    path: str
    revision: int
    state_id: str
    supports_delta: bool

    def load(self, validate: Callable[[dict[str, Any]], None] | None = None) -> LoadResult: ...
    def save(self, payload: dict[str, Any], reason: str = "save", **kw: Any) -> SaveResult: ...
    def peek_revision(self) -> int | None: ...
    def begin(self, label: str = "write") -> None: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...
    def fsck(self) -> dict[str, Any]: ...
    def export(self, dest: str) -> dict[str, Any]: ...
    def import_(self, src: str) -> dict[str, Any]: ...


# ------------------------------------------------------------------ JSON 后端


class JsonStore:
    """单文件快照 + 追加日志 + 跨进程锁。适合冷备、可移植、以及旧库兼容。

    写一次的顺序是固定的，且**日志先于快照**：

        1. 加锁（跨进程）
        2. 追加日志并 fsync      ← 到这里为止，这次提交已经不可撤销
        3. 原子替换快照
        4. 更新 .rev 提示文件

    任何一步崩掉，下次启动都能得出正确结论：
      · 崩在 2 之前 → 这次提交等于没发生（调用方也没收到成功）
      · 崩在 2 与 3 之间 → 日志里有、快照里没有 → 从日志前滚
      · 崩在 3 与 4 之间 → 快照与日志一致，只是提示文件旧了 → 以快照为准
    """

    supports_delta = False

    def __init__(
        self,
        path: str,
        *,
        lock_timeout: float = 10.0,
        journal_enabled: bool = True,
        journal_keep: int = 32,
        verify: bool = True,
        validate: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.path = os.path.abspath(os.path.expanduser(path))
        self.journal_path = self.path + ".journal"
        self.lock_path = self.path + ".lock"
        self.rev_path = self.path + ".rev"
        self.verify = verify
        self.validate = validate
        self.journal = Journal(self.journal_path, keep=journal_keep, enabled=journal_enabled)
        self.lock = FileLock(self.lock_path, timeout=lock_timeout, label="store")
        self.revision = 0
        self.state_id = ""
        self.writer = f"{os.getpid()}@{_host()}"
        self.recovered = False
        self.stats: dict[str, int] = {"saves": 0, "loads": 0, "recoveries": 0, "conflicts": 0}
        self._in_txn = False

    # ---- 能力 ----

    def exists(self) -> bool:
        # 快照没了但日志还在，仍然算「有库」：那是一条可以从日志前滚的库，
        # 把它当成空库等于主动丢掉所有记忆。
        if os.path.exists(self.path):
            return True
        return bool(self.journal.enabled and os.path.exists(self.journal_path))

    def begin(self, label: str = "write") -> None:
        if self._in_txn:
            return
        self.lock.label = label
        self.lock.acquire()
        self._in_txn = True

    def commit(self) -> None:
        if not self._in_txn:
            return
        self._in_txn = False
        self.lock.release()

    def rollback(self) -> None:
        self.commit()

    @contextmanager
    def write_session(self, label: str = "write") -> Iterator["JsonStore"]:
        self.begin(label)
        try:
            yield self
        except BaseException:
            self.rollback()
            raise
        else:
            self.commit()

    # ---- 读 ----

    def peek_revision(self) -> int | None:
        """只读快照头部：判断「别人有没有写过」的最便宜方式（~0.1ms）。"""
        try:
            with open(self.path, "rb") as f:
                head = f.read(4096)
        except OSError:
            return None
        marker = b'"revision"'
        at = head.find(marker)
        if at < 0:
            return None
        tail = head[at + len(marker) :]
        digits = bytearray()
        started = False
        for byte in tail:
            if 48 <= byte <= 57 or (byte == 45 and not started):
                digits.append(byte)
                started = True
            elif started:
                break
        if not digits:
            return None
        try:
            return int(bytes(digits))
        except ValueError:
            return None

    def load(self, validate: Callable[[dict[str, Any]], None] | None = None) -> LoadResult:
        checker = validate or self.validate
        self.stats["loads"] += 1
        snapshot: LoadResult | None = None
        problem = ""
        if os.path.exists(self.path):
            try:
                snapshot = self._read_snapshot(checker)
            except (StoreError, ValueError, OSError) as exc:
                problem = str(exc)
        else:
            problem = "快照不存在"

        if snapshot is None or self._journal_ahead(snapshot.revision):
            recovered = self._recover_from_journal(checker)
            if recovered is not None:
                self.recovered = True
                self.stats["recoveries"] += 1
                self.revision = recovered.revision
                self.state_id = recovered.state_id
                return recovered
        if snapshot is None:
            raise StoreCorrupt(self.path, problem or "无可用快照与日志")
        self.revision = snapshot.revision
        self.state_id = snapshot.state_id
        return snapshot

    def _read_snapshot(self, checker: Callable[[dict[str, Any]], None] | None) -> LoadResult:
        with open(self.path, "rb") as f:
            raw = f.read()
        try:
            state = json.loads(raw)
        except ValueError as exc:
            raise StoreError(f"快照不是合法 JSON: {exc}") from None
        if not isinstance(state, dict):
            raise StoreError("快照顶层不是对象")
        checksum = str(state.get("checksum") or "")
        # 只有 v7+ 才写过校验和。老快照被复制/改动时带上一个陈旧的 checksum，
        # 若照验不误会把「正常的历史迁移」判成「文件损坏」—— 版本得先说话。
        if checksum and self.verify and int(state.get("version") or 0) >= 7:
            payload_only = {k: v for k, v in state.items() if k != "checksum"}
            body = json.dumps(
                payload_only, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            if _sha256(body) != checksum:
                raise StoreError("快照校验和不符（内容被改动或损坏）")
        if checker:
            checker(state)
        return LoadResult(
            payload=state,
            revision=int(state.get("revision") or 0),
            state_id=str(state.get("state_id") or ""),
        )

    def _journal_ahead(self, snapshot_revision: int) -> bool:
        """快照是否落后于日志。用 `.rev` 提示文件做 O(1) 判断，缺了才去扫日志。"""
        if not self.journal.enabled or not os.path.exists(self.journal_path):
            return False
        try:
            hint = read_json(self.rev_path)
        except (OSError, ValueError):
            hint = {}
        hinted = int(hint.get("revision") or -1) if hint else -1
        if hinted == snapshot_revision:
            return False
        records = self.journal.metadata(limit=1)
        if not records:
            return False
        return int(records[-1]["revision"]) > snapshot_revision

    def _recover_from_journal(
        self, checker: Callable[[dict[str, Any]], None] | None
    ) -> LoadResult | None:
        latest = self.journal.latest()
        if latest is None:
            return None
        payload, rec = latest
        if checker:
            checker(payload)
        try:
            self._write_snapshot(payload, revision=rec.revision, reason="recover")
        except OSError:
            pass
        return LoadResult(
            payload=payload,
            revision=rec.revision,
            state_id=str(payload.get("state_id") or ""),
            source="journal",
            recovered=True,
            note=f"从日志第 {rec.seq} 条前滚（revision {rec.revision}）",
        )

    # ---- 写 ----

    def _commit(
        self, payload: dict[str, Any], revision: int, reason: str, *, journal: bool = True
    ) -> tuple[bool, int, int | None]:
        # 快照保持「扁平 JSON」：顶层就是 config / graph / events / …
        # 这样 `cat store.json | jq .config` 依然可用 —— 一个要被人读的文件，
        # 不该为了校验和套一层信封、把最常用的一眼可见变成 payload.config。
        state = dict(payload)
        state["revision"] = int(revision)
        state["state_id"] = self.state_id or str(uuid.uuid4())
        state["written_at"] = round(time.time(), 3)
        state["writer"] = self.writer
        body_for_hash = json.dumps(
            state, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        state["checksum"] = _sha256(body_for_hash)
        # revision 放在最前面：`peek_revision` 只读前 4KB 就能拿到它
        ordered = {"revision": state.pop("revision")}
        ordered.update(state)
        text = json.dumps(ordered, ensure_ascii=False, indent=2)
        seq: int | None = None
        if journal and self.journal.enabled:
            seq = self.journal.append(
                text.encode("utf-8"), revision=revision, reason=reason, writer=self.writer
            )
        durable = atomic_write_text(self.path, text)
        self.revision = int(revision)
        self.state_id = ordered["state_id"]
        self._write_rev_hint()
        return durable, len(text.encode("utf-8")), seq

    def _write_snapshot(self, payload: dict[str, Any], revision: int, reason: str) -> tuple[bool, int]:
        durable, size, _ = self._commit(payload, revision, reason, journal=False)
        return durable, size

    def _write_rev_hint(self) -> None:
        try:
            atomic_write_text(
                self.rev_path,
                json.dumps(
                    {
                        "revision": self.revision,
                        "state_id": self.state_id,
                        "at": round(time.time(), 3),
                    },
                    ensure_ascii=False,
                ),
            )
        except OSError:
            pass

    def save(self, payload: dict[str, Any], reason: str = "save", **kw: Any) -> SaveResult:
        started = time.perf_counter()
        own = not self._in_txn
        if own:
            self.begin(reason)
        try:
            revision = self.revision + 1
            durable, size, seq = self._commit(payload, revision, reason)
            self.stats["saves"] += 1
            return SaveResult(
                ok=True,
                durable=durable,
                revision=revision,
                bytes=size,
                elapsed_ms=(time.perf_counter() - started) * 1000,
                journal_seq=seq,
                state_id=self.state_id,
                nodes_written=len(payload.get("graph", {}).get("nodes", [])),
            )
        finally:
            if own:
                self.commit()

    # ---- 运维 ----

    def history(self, limit: int = 20) -> list[dict[str, Any]]:
        return self.journal.metadata(limit=limit)

    def state_at(self, revision: int) -> dict[str, Any] | None:
        best: dict[str, Any] | None = None
        for rec, payload in self.journal.iter_records(with_state=True):
            if payload is not None and rec.revision <= revision:
                try:
                    best = json.loads(payload.decode("utf-8"))
                except ValueError:
                    continue
        return best

    def recover(self) -> dict[str, Any]:
        try:
            result = self.load()
            return {
                "ok": True,
                "source": result.source,
                "revision": result.revision,
                "recovered": result.recovered,
                "note": result.note,
            }
        except StoreCorrupt as exc:
            return {"ok": False, "error": str(exc)}

    def fsck(self) -> dict[str, Any]:
        report: dict[str, Any] = {
            "backend": "json",
            "path": self.path,
            "exists": os.path.exists(self.path),
            "journal": self.journal.integrity() if self.journal.enabled else {"enabled": False},
            "lock": holder_of(self.lock_path),
        }
        if report["exists"]:
            report["bytes"] = os.path.getsize(self.path)
            try:
                result = self.load()
                report.update(
                    {
                        "ok": True,
                        "revision": result.revision,
                        "source": result.source,
                        "nodes": len(result.payload.get("graph", {}).get("nodes", [])),
                        "version": result.payload.get("version"),
                        "state_id": result.state_id,
                    }
                )
            except (StoreError, ValueError) as exc:
                report.update({"ok": False, "error": str(exc)})
        else:
            report["ok"] = False
            report["error"] = "快照不存在"
            if self.journal.enabled and os.path.exists(self.journal_path):
                report["journal_recoverable"] = self.journal.latest() is not None
        return report

    def repair(self) -> dict[str, Any]:
        """只在体检失败时用：用日志重建快照。**不删日志、不删旧快照**。"""
        backup = ""
        if os.path.exists(self.path):
            backup = f"{self.path}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}"
            try:
                shutil.copy2(self.path, backup)
            except OSError as exc:
                return {"ok": False, "error": f"无法备份损坏快照: {exc}"}
        latest = self.journal.latest()
        if latest is None:
            return {"ok": False, "error": "日志里没有可用记录，无法重建", "backup": backup}
        payload, rec = latest
        self.begin("repair")
        try:
            durable, size = self._write_snapshot(payload, revision=rec.revision, reason="repair")
            self.commit()
        except BaseException:
            self.rollback()
            raise
        return {"ok": True, "backup": backup, "revision": rec.revision, "bytes": size, "durable": durable}

    def export(self, dest: str, include_journal: bool = True) -> dict[str, Any]:
        result = self.load()
        return _write_bundle(
            dest,
            result.payload,
            revision=result.revision,
            state_id=result.state_id,
            source=self.path,
            history=self.journal.metadata(limit=200) if include_journal else [],
        )

    def import_(self, src: str) -> dict[str, Any]:
        payload, meta = _read_bundle(src)
        if self.validate:
            self.validate(payload)
        backup = _backup_before(self.path)
        self.begin("import")
        try:
            self.state_id = str(meta.get("state_id") or uuid.uuid4())
            durable, size = self._write_snapshot(
                payload, revision=int(meta.get("revision") or 0), reason="import"
            )
            self.commit()
        except BaseException:
            self.rollback()
            raise
        return {
            "ok": True,
            "revision": self.revision,
            "bytes": size,
            "backup": backup,
            "durable": durable,
        }

    def describe(self) -> dict[str, Any]:
        return {
            "backend": "json",
            "path": self.path,
            "journal": self.journal_path,
            "lock": self.lock_path,
            "revision": self.revision,
            "state_id": self.state_id,
            "stats": dict(self.stats),
            "journal_enabled": self.journal.enabled,
            "journal_keep": self.journal.keep,
        }

    def close(self) -> None:
        if self._in_txn:
            self.commit()


# ------------------------------------------------------------------ 工厂


def detect_backend(path: str) -> str:
    low = path.lower()
    if low.endswith((".sqlite", ".sqlite3", ".db")):
        return "sqlite"
    if low.endswith(".json"):
        return "json"
    # 没有扩展名就看看旁边有什么：有 sqlite 用 sqlite，有 json 用 json
    for suffix, backend in ((".sqlite", "sqlite"), (".json", "json")):
        if os.path.exists(path + suffix) or os.path.exists(path + suffix + ".journal"):
            return backend
    return "sqlite"


def open_store(
    path: str,
    backend: str = "auto",
    *,
    lock_timeout: float = 10.0,
    journal_enabled: bool = True,
    journal_keep: int = 32,
    verify: bool = True,
    validate: Callable[[dict[str, Any]], None] | None = None,
    snapshot_every: int = 50,
    synchronous: str = "FULL",
) -> Store:
    kind = detect_backend(path) if backend in ("", "auto") else backend
    if kind == "sqlite":
        from .sqlite_store import SqliteStore

        return SqliteStore(
            path,
            timeout=lock_timeout,
            snapshot_every=snapshot_every,
            synchronous=synchronous,
            validate=validate,
        )
    if kind == "json":
        return JsonStore(
            path,
            lock_timeout=lock_timeout,
            journal_enabled=journal_enabled,
            journal_keep=journal_keep,
            verify=verify,
            validate=validate,
        )
    raise ValueError(f"未知后端: {backend!r}（可用 sqlite / json）")


# ------------------------------------------------------------------ 共用工具


def _host() -> str:
    import socket

    return socket.gethostname()


def remove_store_files(path: str) -> list[str]:
    """删掉一个库的**全部**落地文件（快照、日志、提示、锁、WAL）。

    为什么专门给一个函数：只删 `store.json` 会留下 journal，
    下次打开时「快照没了但日志还在」会被正确地判成崩溃并前滚 ——
    于是你以为清空了，实际恢复出上一次的全部内容。这个坑真实发生过（demo 里）。
    """
    removed: list[str] = []
    for suffix in ("", ".journal", ".rev", ".lock", "-wal", "-shm", ".sqlite-wal", ".sqlite-shm"):
        candidate = path + suffix
        if os.path.exists(candidate):
            try:
                os.unlink(candidate)
                removed.append(candidate)
            except OSError:
                pass
    return removed


def _backup_before(path: str) -> str:
    if not os.path.exists(path):
        return ""
    backup = f"{path}.before-import-{time.strftime('%Y%m%d-%H%M%S')}"
    try:
        shutil.copy2(path, backup)
    except OSError:
        return ""
    return backup


def _write_bundle(
    dest: str,
    payload: dict[str, Any],
    *,
    revision: int,
    state_id: str,
    source: str,
    history: list[dict[str, Any]],
) -> dict[str, Any]:
    """导出一份可搬走的档案：一个文件，自带校验和与来源信息。

    「不可再生数据」的最后一环是**能离开这台机器**。不能导出的持久化，
    只是「暂时还没丢」。
    """
    bundle = {
        "format": "memory-palace/export",
        "format_version": 1,
        "exported_at": time.time(),
        "source": source,
        "state_id": state_id,
        "revision": revision,
        "history": history,
        "payload": payload,
    }
    body = json.dumps(bundle, ensure_ascii=False, indent=2)
    bundle_sha = _sha256(body.encode("utf-8"))
    text = json.dumps(
        {"format": "memory-palace/export", "sha256": bundle_sha, "payload": bundle},
        ensure_ascii=False,
        indent=2,
    )
    atomic_write_text(dest, text)
    return {
        "ok": True,
        "dest": dest,
        "revision": revision,
        "bytes": os.path.getsize(dest),
        "sha256": bundle_sha,
    }


def _read_bundle(src: str) -> tuple[dict[str, Any], dict[str, Any]]:
    with open(src, "rb") as f:
        doc = json.loads(f.read())
    sha = str(doc.get("sha256") or "")
    bundle = doc.get("payload") or {}
    body = json.dumps(bundle, ensure_ascii=False, indent=2)
    if sha and _sha256(body.encode("utf-8")) != sha:
        raise StoreError("档案校验和不符，拒绝导入")
    payload = bundle.get("payload")
    if not isinstance(payload, dict):
        raise StoreError("档案里没有 payload")
    return payload, bundle
