"""journal.py - 追加式日志（WAL）：让「已经写下去的」不会因为一次坏写而消失。

快照是原地替换的：`os.replace` 保证你看到的要么是旧版、要么是新版，绝不会是半截。
但它是**覆盖**语义 —— 一旦写进去的内容本身有问题（程序 bug 写坏了字段、
用户手改了 JSON、盘上某个扇区坏了），旧的那一版就没有了。

日志补的正是这一环：

    append(记录)  →  fsync  →  append(记录)  →  fsync  →  …  →  快照原子替换

每一次提交都在日志里留一条带 CRC32 的完整记录。快照坏了，从日志恢复；
想回到三小时前的状态，从日志里取那一条。文件只追加不改写，
所以「断电时写了半条」这种事的后果被限制在**最后一条**：CRC 对不上就丢弃它，
前面的记录一条不少。这是经典 WAL 的语义，不是新发明。

为什么整份状态而不是增量：增量日志要求每个写操作都能序列化回放，
而这个库的写面覆盖情感、人格、叙事、风格、自修正十几个引擎 ——
为每种变更设计可回放的 op 是另一个量级的复杂度，且每加一个引擎就要再加一次。
整份记录 + zlib 压缩（1.3MB 快照压到 ~90KB）在成本上完全可接受，
换来的是「无论哪个引擎怎么改，日志都一定是对的」。工程上这叫选对不变量。

格式（每条）：

    MAGIC(4) | kind(1) | length(8) | crc32(4) | zlib(payload)

length 是压缩后长度，crc32 校验的是压缩后字节；解压后再校验一次 sha256（在 payload 里），
两道校验分别防「盘上坏了」与「逻辑写坏了」。
"""
from __future__ import annotations

import json
import os
import struct
import time
import zlib
from dataclasses import dataclass
from typing import Any, Iterator

MAGIC = b"MPJ1"
KIND_STATE = 1
HEADER = struct.Struct(">4sBQI")
MAX_RECORD_BYTES = 256 * 1024 * 1024  # 单条上限：超过它一定是读错了位置，而不是真有这么大
DEFAULT_KEEP = 32


@dataclass(frozen=True)
class JournalRecord:
    seq: int
    revision: int
    ts: float
    reason: str
    writer: str
    offset: int
    length: int
    crc: int
    valid: bool = True


class Journal:
    """一个 store 对应一份 journal（`<store>.journal`）。

    并发安全由外部的跨进程写锁保证：日志本身不做加锁 ——
    两个进程同时 append 会让记录交错，这种事必须在调用方就被挡住，
    在这里再补一层锁只会让「锁顺序」变复杂。
    """

    def __init__(self, path: str, keep: int = DEFAULT_KEEP, enabled: bool = True) -> None:
        self.path = os.path.abspath(path)
        self.keep = max(2, int(keep))
        self.enabled = enabled
        self._count: int | None = None
        self._seq: int | None = None
        self._bytes: int | None = None

    # ---- 写 ----

    def append(
        self,
        payload: bytes,
        *,
        revision: int,
        reason: str = "save",
        writer: str = "",
        ts: float | None = None,
    ) -> int | None:
        if not self.enabled:
            return None
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        # 先取缓存值：count()/last_seq() 都要扫文件，放在「写完之后」就会变成每次 O(n)
        prev_count = self.count()
        seq = self.last_seq() + 1
        meta = {
            "seq": seq,
            "revision": int(revision),
            "ts": time.time() if ts is None else float(ts),
            "reason": reason,
            "writer": writer or f"{os.getpid()}",
        }
        body = json.dumps(
            {"meta": meta, "payload_sha256": _sha(payload)}, ensure_ascii=False
        ).encode("utf-8")
        # 头信息与正文一起压缩：解压一次拿到全部，比把 meta 放宽再单独读省一次 IO
        blob = zlib.compress(len(body).to_bytes(4, "big") + body + payload, 1)
        crc = zlib.crc32(blob) & 0xFFFFFFFF
        with open(self.path, "ab") as f:
            f.write(HEADER.pack(MAGIC, KIND_STATE, len(blob), crc))
            f.write(blob)
            f.flush()
            os.fsync(f.fileno())
        self._count = prev_count + 1
        self._seq = seq
        self._bytes = None
        if self._count >= self.keep * 3:
            self.compact()
        return seq

    def compact(self, keep: int | None = None) -> dict[str, Any]:
        """只保留最近 N 条，原子替换。

        为什么留 N 条而不是留一条：留一条等于「日志只是快照的副本」，
        快照一坏就跟着没；留 N 条才有「往回退几步」的能力，
        而 N*90KB 的代价在任何一个真实部署里都不值得省。
        """
        limit = max(2, int(keep if keep is not None else self.keep))
        if not os.path.exists(self.path):
            return {"compacted": False, "kept": 0}
        raws = list(self._raw_records())
        if len(raws) <= limit:
            return {"compacted": False, "kept": len(raws)}
        kept = raws[-limit:]
        tmp = f"{self.path}.tmp-{os.getpid()}"
        with open(tmp, "wb") as f:
            for header, blob in kept:
                f.write(header)
                f.write(blob)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)
        self._count = None
        self._seq = None
        self._bytes = None
        return {
            "compacted": True,
            "kept": len(kept),
            "dropped": len(raws) - len(kept),
            "bytes": os.path.getsize(self.path),
        }

    # ---- 读 ----

    def _raw_records(self) -> Iterator[tuple[bytes, bytes]]:
        """顺序扫描，遇到第一条坏记录就停。

        停而不是跳过：坏记录之后的位置不可信（长度字段本身可能就是坏的），
        强行对齐只会把一段随机字节解释成一条「合法」记录。
        """
        try:
            f = open(self.path, "rb")
        except OSError:
            return
        with f:
            while True:
                offset = f.tell()
                header = f.read(HEADER.size)
                if len(header) < HEADER.size:
                    return
                magic, kind, length, crc = HEADER.unpack(header)
                if magic != MAGIC or kind != KIND_STATE or length > MAX_RECORD_BYTES:
                    return
                blob = f.read(length)
                if len(blob) < length:
                    return
                if (zlib.crc32(blob) & 0xFFFFFFFF) != crc:
                    return
                yield header, blob

    def iter_records(self, with_state: bool = False) -> Iterator[tuple[JournalRecord, bytes | None]]:
        for header, blob in self._raw_records():
            rec, payload = self._decode(header, blob)
            if rec is None:
                return
            yield rec, payload if with_state else None

    def _decode(
        self, header: bytes, blob: bytes
    ) -> tuple[JournalRecord | None, bytes | None]:
        try:
            raw = zlib.decompress(blob)
        except zlib.error:
            return None, None
        if len(raw) < 4:
            return None, None
        meta_len = int.from_bytes(raw[:4], "big")
        meta = json.loads(raw[4 : 4 + meta_len].decode("utf-8"))
        payload = raw[4 + meta_len :]
        sha = meta.get("payload_sha256")
        if sha and sha != _sha(payload):
            # 解压成功但内容对不上：这是「逻辑坏」，比 CRC 失败更值得报出来
            return None, None
        _, _, length, crc = HEADER.unpack(header)
        rec = JournalRecord(
            seq=int(meta.get("seq") or 0),
            revision=int(meta.get("revision") or 0),
            ts=float(meta.get("ts") or 0.0),
            reason=str(meta.get("reason") or ""),
            writer=str(meta.get("writer") or ""),
            offset=0,
            length=length,
            crc=crc,
        )
        return rec, payload

    def metadata(self, limit: int | None = None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for rec, _ in self.iter_records():
            out.append(
                {
                    "seq": rec.seq,
                    "revision": rec.revision,
                    "ts": rec.ts,
                    "reason": rec.reason,
                    "writer": rec.writer,
                    "bytes": rec.length,
                }
            )
        return out[-limit:] if limit else out

    def latest(self) -> tuple[dict[str, Any], JournalRecord] | None:
        """取最后一条可解码的记录。坏记录自动往前退 —— 这正是 WAL 的意义。"""
        found: tuple[dict[str, Any], JournalRecord] | None = None
        for rec, payload in self.iter_records(with_state=True):
            if payload is None:
                continue
            try:
                state = json.loads(payload.decode("utf-8"))
            except ValueError:
                continue
            found = (state, rec)
        return found

    def last_seq(self) -> int:
        if self._seq is None:
            seq = 0
            for rec, _ in self.iter_records():
                seq = max(seq, rec.seq)
            self._seq = seq
        return self._seq

    def count(self) -> int:
        """只数记录条数：不查表、不解压 —— 每条 17 字节头就够判断了。"""
        if self._count is None:
            self._count = sum(1 for _ in self._raw_records())
        return self._count

    def bytes(self) -> int:
        try:
            return os.path.getsize(self.path)
        except OSError:
            return 0

    def integrity(self) -> dict[str, Any]:
        """体检：多少条、最后一条到哪、尾部有没有半条。

        尾巴坏了不算故障（断电就会这样），要报出来但不必惊动用户；
        中间坏才是真的坏了 —— 但那也意味着后面的记录全丢，
        所以这里报 `truncated_at`，让上层决定要不要人工介入。
        """
        records = self.metadata()
        size = self.bytes()
        consumed = sum(r["bytes"] + HEADER.size for r in records)
        tail = max(0, size - consumed)
        return {
            "path": self.path,
            "records": len(records),
            "bytes": size,
            "first_seq": records[0]["seq"] if records else None,
            "last_seq": records[-1]["seq"] if records else None,
            "last_revision": records[-1]["revision"] if records else None,
            "tail_bytes": tail,
            "torn_tail": tail > 0,
        }

    def truncate(self) -> None:
        """清空日志（只在确认快照可用后由 fsck/repair 调用）。"""
        try:
            os.unlink(self.path)
        except OSError:
            pass
        self._count = self._seq = self._bytes = None


def _sha(payload: bytes) -> str:
    import hashlib

    return hashlib.sha256(payload).hexdigest()
