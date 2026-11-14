"""sqlite_store.py - SQLite（WAL）后端：行级存储、单写多读、崩溃自前滚。

为什么它是这个库的默认后端，而 JSON 退居「可移植/备份」：

  · **并发**：WAL 允许多个读者与一个写者同时工作。写者之间用 `BEGIN IMMEDIATE`
    排队，而不是互相覆盖。以前两个进程各写各的 JSON，最后一次覆盖前面所有修改，
    且没有任何一方报错。
  · **扩展性**：一条记忆是一行。改一条写一行，而不是重写 1.3MB 的整份快照。
    节点表按 scope / tier / valid_to / updated_rev 建了索引，
    「本项目里还有效的记忆」不需要全表扫。
  · **不可再生**：`commits` 表是只增不改的提交账本；`events` 表只追加不删除
    （内存里的事件会被 trim 掉，库里的不会）。要恢复、要审计、要回答
    「上周三那次修改之前是什么样」，都从这里取。
  · **零依赖**：`sqlite3` 是标准库。

表结构（v1）：

    meta        单行键值：schema / revision / state_id / payload_version
    nodes       一条记忆一行：name 主键 + json + 常用查询列 + updated_rev
    edges       共现边：(a,b) 主键 + json + updated_rev
    tombstones  墓碑：被显式丢弃的记忆（不参与检索，但可 restore）
    blobs       引擎状态：style / narrative / persona / selfcorrect / …（小、整体替换）
    events      只追加的事件流（审计与反思的原料）
    commits     提交账本：每次提交一行，含原因、写者、写入节点数、摘要
    snapshots   每 N 次提交留一份完整快照（时间旅行用，只留最近几份）

增量写：`save(payload, delta=True)` 只写 `nodes` 里给出的脏节点、`removed` 里给出的名字、
以及变化了的 blob/edge。没变的行一个字节都不碰 —— 这是「改一条写一行」的落地方式。
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from typing import Any, Callable

from .locking import LockBusy, holder_of
from .store import (
    LoadResult,
    SaveResult,
    StoreCorrupt,
    StoreEmpty,
    _backup_before,
    _read_bundle,
    _sha256,
    _write_bundle,
)

SCHEMA_VERSION = 1
DEFAULT_EVENT_LIMIT = 5000
BLOB_KEYS = (
    "config",
    "logic",
    "style",
    "narrative",
    "foundation",
    "personality",
    "persona",
    "selfcorrect",
    "last_report",
)

_DDL = """
CREATE TABLE IF NOT EXISTS meta(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS nodes(
  name TEXT PRIMARY KEY,
  json TEXT NOT NULL,
  scope TEXT,
  tier TEXT,
  kind TEXT,
  pinned INTEGER DEFAULT 0,
  created_at REAL,
  valid_from REAL,
  valid_to REAL,
  updated_rev INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS nodes_scope_idx ON nodes(scope);
CREATE INDEX IF NOT EXISTS nodes_tier_idx ON nodes(tier);
CREATE INDEX IF NOT EXISTS nodes_valid_idx ON nodes(valid_to);
CREATE INDEX IF NOT EXISTS nodes_rev_idx ON nodes(updated_rev);
CREATE TABLE IF NOT EXISTS edges(
  a TEXT NOT NULL,
  b TEXT NOT NULL,
  json TEXT NOT NULL,
  updated_rev INTEGER NOT NULL,
  PRIMARY KEY(a, b)
);
CREATE INDEX IF NOT EXISTS edges_rev_idx ON edges(updated_rev);
CREATE TABLE IF NOT EXISTS tombstones(
  name TEXT PRIMARY KEY,
  json TEXT NOT NULL,
  updated_rev INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS blobs(
  key TEXT PRIMARY KEY,
  json TEXT NOT NULL,
  updated_rev INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS events(
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  kind TEXT NOT NULL,
  json TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS events_dedup_idx ON events(ts, kind);
CREATE INDEX IF NOT EXISTS events_ts_idx ON events(ts);
CREATE TABLE IF NOT EXISTS commits(
  rev INTEGER PRIMARY KEY,
  ts REAL NOT NULL,
  reason TEXT,
  writer TEXT,
  nodes_written INTEGER DEFAULT 0,
  nodes_total INTEGER DEFAULT 0,
  bytes INTEGER DEFAULT 0,
  digest TEXT,
  state_id TEXT
);
CREATE TABLE IF NOT EXISTS snapshots(
  rev INTEGER PRIMARY KEY,
  ts REAL NOT NULL,
  json TEXT NOT NULL
);
"""


class SqliteStore:
    supports_delta = True

    def __init__(
        self,
        path: str,
        *,
        timeout: float = 10.0,
        snapshot_every: int = 50,
        snapshot_keep: int = 3,
        synchronous: str = "FULL",
        event_limit: int = DEFAULT_EVENT_LIMIT,
        validate: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.path = os.path.abspath(os.path.expanduser(path))
        self.timeout = max(0.1, float(timeout))
        self.snapshot_every = max(0, int(snapshot_every))
        self.snapshot_keep = max(1, int(snapshot_keep))
        self.synchronous = synchronous if synchronous in ("FULL", "NORMAL", "OFF") else "FULL"
        self.event_limit = int(event_limit)
        self.validate = validate
        self.revision = 0
        self.state_id = ""
        self.stats: dict[str, int] = {
            "saves": 0,
            "loads": 0,
            "nodes_upserted": 0,
            "edges_upserted": 0,
            "conflicts": 0,
        }
        self._conn: sqlite3.Connection | None = None
        self._in_txn = False
        self._node_cache: dict[str, str] = {}
        self._edge_cache: dict[tuple[str, str], str] | None = None
        self._blob_cache: dict[str, str] = {}
        self._tomb_cache: dict[str, str] = {}

    # ---- 连接与建表 ----

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            parent = os.path.dirname(self.path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            conn = sqlite3.connect(
                self.path, timeout=self.timeout, isolation_level=None, check_same_thread=False
            )
            conn.execute(f"PRAGMA busy_timeout={int(self.timeout * 1000)}")
            conn.execute("PRAGMA synchronous=" + self.synchronous)
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            if str(mode).lower() != "wal":
                # WAL 是这个后端存在的理由：没有它就是「一个进程写、其他人干等」。
                conn.execute("PRAGMA journal_mode=WAL")
            for attempt in range(20):
                try:
                    conn.executescript(_DDL)
                    break
                except sqlite3.OperationalError as exc:
                    text = str(exc).lower()
                    if ("locked" in text or "readonly" in text or "busy" in text) and attempt < 19:
                        time.sleep(0.02 * (attempt + 1))
                        continue
                    raise
            self._conn = conn
            self._load_meta()
        return self._conn

    def _load_meta(self) -> None:
        try:
            rows = dict(self._conn.execute("SELECT key, value FROM meta").fetchall())  # type: ignore[union-attr]
        except sqlite3.OperationalError:
            return
        self.revision = int(rows.get("revision") or 0)
        self.state_id = str(rows.get("state_id") or "")

    def _meta(self, key: str, default: str = "") -> str:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return default if row is None else str(row[0])

    def _set_meta(self, key: str, value: Any) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )

    def exists(self) -> bool:
        """有没有「已经提交过的记忆」。

        不能只看文件存在：`sqlite3.connect` 会立刻创建文件，另一个进程此时
        还没写第一笔。把这种「刚建好、还是空的」当成有库，就会去 load 一个空库，
        进而把它判成损坏 —— 并发启动时第二个进程必崩，而这跟数据好坏毫无关系。
        """
        if not os.path.exists(self.path) or os.path.getsize(self.path) == 0:
            return False
        try:
            row = self.conn.execute("SELECT value FROM meta WHERE key='revision'").fetchone()
        except sqlite3.Error:
            return False
        return row is not None

    # ---- 事务 ----

    def begin(self, label: str = "write") -> None:
        if self._in_txn:
            return
        try:
            self.conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc) or "busy" in str(exc):
                raise LockBusy(self.path, self.timeout, {"label": label}) from None
            raise
        self._in_txn = True

    def commit(self) -> None:
        if not self._in_txn:
            return
        self._in_txn = False
        self.conn.execute("COMMIT")

    def rollback(self) -> None:
        if not self._in_txn:
            return
        self._in_txn = False
        try:
            self.conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass

    # ---- 读 ----

    def peek_revision(self) -> int | None:
        if not os.path.exists(self.path):
            return None
        try:
            row = self.conn.execute("SELECT value FROM meta WHERE key='revision'").fetchone()
        except sqlite3.OperationalError:
            return None
        return int(row[0]) if row else 0

    def load(self, validate: Callable[[dict[str, Any]], None] | None = None) -> LoadResult:
        conn = self.conn
        self.stats["loads"] += 1
        try:
            meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
        except sqlite3.OperationalError as exc:
            raise StoreCorrupt(self.path, f"读取 meta 失败: {exc}") from None
        if not meta:
            # 另一个进程可能正在建库：给它一点时间提交第一笔，再下结论
            for _ in range(10):
                time.sleep(0.02)
                meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
                if meta:
                    break
        if not meta:
            raise StoreEmpty(self.path)
        blobs: dict[str, Any] = {}
        for key, value in conn.execute("SELECT key, json FROM blobs"):
            try:
                blobs[key] = json.loads(value)
            except ValueError:
                raise StoreCorrupt(self.path, f"blob 损坏: {key}") from None
        if "config" not in blobs:
            raise StoreCorrupt(self.path, "缺少 config（库不是这个版本写的？）")
        nodes = []
        self._node_cache.clear()
        for name, raw in conn.execute("SELECT name, json FROM nodes"):
            self._node_cache[name] = raw
            try:
                nodes.append(json.loads(raw))
            except ValueError:
                raise StoreCorrupt(self.path, f"节点损坏: {name}") from None
        edges = []
        self._edge_cache = {}
        for a, b, raw in conn.execute("SELECT a, b, json FROM edges"):
            self._edge_cache[(a, b)] = raw
            try:
                edges.append([a, b, json.loads(raw)])
            except ValueError:
                raise StoreCorrupt(self.path, f"边损坏: {a}~{b}") from None
        tombstones = []
        for name, raw in conn.execute("SELECT name, json FROM tombstones"):
            self._tomb_cache[name] = raw
            try:
                tombstones.append(json.loads(raw))
            except ValueError:
                raise StoreCorrupt(self.path, f"墓碑损坏: {name}") from None
        events: list[Any] = []
        for (raw,) in conn.execute(
            "SELECT json FROM events ORDER BY seq DESC LIMIT ?", (self.event_limit,)
        ):
            try:
                events.append(json.loads(raw))
            except ValueError:
                continue
        events.reverse()
        self._blob_cache = {k: json.dumps(v, ensure_ascii=False) for k, v in blobs.items()}
        payload: dict[str, Any] = {
            "version": int(meta.get("payload_version") or 0),
            "saved_at": float(meta.get("saved_at") or 0.0),
            **{k: blobs[k] for k in BLOB_KEYS if k in blobs},
            "graph": {"nodes": nodes, "edges": edges},
            "events": events,
            "tombstone": tombstones,
            "last_report": blobs.get("last_report"),
        }
        if validate:
            validate(payload)
        self.revision = int(meta.get("revision") or 0)
        self.state_id = str(meta.get("state_id") or "")
        return LoadResult(
            payload=payload, revision=self.revision, state_id=self.state_id, source="sqlite"
        )

    # ---- 写 ----

    def save(
        self,
        payload: dict[str, Any],
        reason: str = "save",
        *,
        delta: bool = False,
        base_revision: int | None = None,
    ) -> SaveResult:
        started = time.perf_counter()
        own = not self._in_txn
        if own:
            self.begin(reason)
        try:
            conn = self.conn
            db_rev = int(self._meta("revision", "0") or 0)
            rebased = 0
            if base_revision is not None and db_rev > base_revision:
                # 别人先提交了。我们只写自己脏掉的行，所以不需要整库重载；
                # 但要如实记账 —— 「我是在别人的基础上写的」是一等事实。
                rebased = db_rev - base_revision
                self.stats["conflicts"] += 1
            rev = db_rev + 1

            if delta:
                nodes = payload.get("nodes") or []
                removed = payload.get("removed") or []
                edges = payload.get("edges")
                blobs = payload.get("blobs") or {}
                events = payload.get("events") or []
                tomb = payload.get("tombstone") or []
                version = payload.get("version")
                saved_at = payload.get("saved_at")
            else:
                graph = payload.get("graph") or {}
                nodes = graph.get("nodes") or []
                removed = []
                edges = graph.get("edges") or []
                blobs = {k: payload.get(k) for k in BLOB_KEYS if k in payload}
                events = payload.get("events") or []
                tomb = payload.get("tombstone") or []
                version = payload.get("version")
                saved_at = payload.get("saved_at")

            written = 0
            digest_parts: list[str] = []
            node_rows = []
            seen_nodes: list[str] = []
            for nd in nodes:
                name = str(nd.get("name") or "")
                if not name:
                    continue
                seen_nodes.append(name)
                raw = json.dumps(nd, ensure_ascii=False, sort_keys=True)
                if self._node_cache.get(name) == raw:
                    continue
                digest_parts.append(f"{name}\x00{raw}")
                node_rows.append(
                    (
                        name,
                        raw,
                        nd.get("scope"),
                        nd.get("tier"),
                        nd.get("kind"),
                        1 if nd.get("pinned") else 0,
                        float(nd.get("created_at") or 0.0),
                        float(nd.get("valid_from") or 0.0),
                        nd.get("valid_to"),
                        rev,
                    )
                )
            if node_rows:
                conn.executemany(
                    "INSERT INTO nodes(name,json,scope,tier,kind,pinned,created_at,valid_from,valid_to,updated_rev)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(name) DO UPDATE SET json=excluded.json, scope=excluded.scope,"
                    " tier=excluded.tier, kind=excluded.kind, pinned=excluded.pinned,"
                    " created_at=excluded.created_at, valid_from=excluded.valid_from,"
                    " valid_to=excluded.valid_to, updated_rev=excluded.updated_rev",
                    node_rows,
                )
                for row in node_rows:
                    self._node_cache[row[0]] = row[1]
                written = len(node_rows)
                self.stats["nodes_upserted"] += written

            if removed:
                conn.executemany("DELETE FROM nodes WHERE name=?", [(n,) for n in removed])
                for name in removed:
                    self._node_cache.pop(name, None)
            if not delta:
                # 全量同步：没在这次 payload 里出现的节点就是被删掉了。
                # 不能按 `updated_rev < rev` 删 —— 未变化的行会被跳过（不写），
                # 它们的 updated_rev 还是旧值，按 rev 删会把它们一起删光。
                # （踩过：重复导入一份相同档案，结果导完库是空的。）
                self._sweep_missing("nodes", "name", [row[0] for row in node_rows] + seen_nodes)

            if edges is not None:
                if self._edge_cache is None:
                    self._edge_cache = {
                        (a, b): raw for a, b, raw in conn.execute("SELECT a,b,json FROM edges")
                    }
                edge_rows = []
                edge_names: list[tuple[str, str]] = []
                for item in edges:
                    a, b, ed = item[0], item[1], item[2]
                    key = (a, b) if a < b else (b, a)
                    edge_names.append(key)
                    raw = json.dumps(ed, ensure_ascii=False, sort_keys=True)
                    if self._edge_cache.get(key) == raw:
                        continue
                    edge_rows.append((key[0], key[1], raw, rev))
                if edge_rows:
                    conn.executemany(
                        "INSERT INTO edges(a,b,json,updated_rev) VALUES(?,?,?,?)"
                        " ON CONFLICT(a,b) DO UPDATE SET json=excluded.json,"
                        " updated_rev=excluded.updated_rev",
                        edge_rows,
                    )
                    for row in edge_rows:
                        self._edge_cache[(row[0], row[1])] = row[2]
                    self.stats["edges_upserted"] += len(edge_rows)
                if not delta:
                    self._sweep_missing(
                        "edges", ("a", "b"), [tuple(key) for key in edge_names]
                    )

            for key, value in blobs.items():
                if value is None:
                    continue
                raw = json.dumps(value, ensure_ascii=False, sort_keys=True)
                if self._blob_cache.get(key) == raw:
                    continue
                conn.execute(
                    "INSERT INTO blobs(key,json,updated_rev) VALUES(?,?,?)"
                    " ON CONFLICT(key) DO UPDATE SET json=excluded.json,"
                    " updated_rev=excluded.updated_rev",
                    (key, raw, rev),
                )
                self._blob_cache[key] = raw

            if tomb:
                rows = []
                for td in tomb:
                    name = str(td.get("name") or "")
                    if not name:
                        continue
                    raw = json.dumps(td, ensure_ascii=False, sort_keys=True)
                    if self._tomb_cache.get(name) == raw:
                        continue
                    rows.append((name, raw, rev))
                if rows:
                    conn.executemany(
                        "INSERT INTO tombstones(name,json,updated_rev) VALUES(?,?,?)"
                        " ON CONFLICT(name) DO UPDATE SET json=excluded.json,"
                        " updated_rev=excluded.updated_rev",
                        rows,
                    )
                    for name, raw, _ in rows:
                        self._tomb_cache[name] = raw

            if events:
                conn.executemany(
                    "INSERT OR IGNORE INTO events(ts,kind,json) VALUES(?,?,?)",
                    [
                        (
                            float(e.get("ts") or 0.0),
                            str(e.get("kind") or ""),
                            json.dumps(e, ensure_ascii=False, sort_keys=True),
                        )
                        for e in events
                    ],
                )

            total = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
            digest = _sha256("".join(sorted(digest_parts)).encode("utf-8")) if digest_parts else ""
            body_bytes = sum(len(row[1]) for row in node_rows)
            conn.execute(
                "INSERT OR REPLACE INTO commits(rev,ts,reason,writer,nodes_written,nodes_total,bytes,digest,state_id)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    rev,
                    time.time(),
                    reason,
                    f"{os.getpid()}@{_host()}",
                    written,
                    int(total),
                    body_bytes,
                    digest,
                    self.state_id or str(uuid.uuid4()),
                ),
            )
            if version is not None:
                self._set_meta("payload_version", int(version))
            if saved_at is not None:
                self._set_meta("saved_at", float(saved_at))
            if not self.state_id:
                self.state_id = str(uuid.uuid4())
            self._set_meta("schema", SCHEMA_VERSION)
            self._set_meta("revision", rev)
            self._set_meta("state_id", self.state_id)
            self._set_meta("updated_at", round(time.time(), 3))

            if self.snapshot_every and rev % self.snapshot_every == 0:
                self._take_snapshot(rev)

            self.revision = rev
            self.stats["saves"] += 1
            if own:
                self.commit()
            return SaveResult(
                ok=True,
                durable=True,
                revision=rev,
                bytes=body_bytes,
                elapsed_ms=(time.perf_counter() - started) * 1000,
                state_id=self.state_id,
                nodes_written=written,
                rebased=rebased,
            )
        except BaseException:
            if own:
                self.rollback()
            raise

    def _sweep_missing(self, table: str, key: str | tuple[str, ...], keys: list) -> int:
        """全量同步时删掉「这次 payload 里没有」的行。

        分批用临时表而不是 `NOT IN (?,?,…)`：SQLite 的变量个数有上限，
        几千个占位符会直接报错，而全量同步恰恰是行数最多的时候。
        """
        cols = (key,) if isinstance(key, str) else tuple(key)
        tmp = f"_seen_{table}"
        conn = self.conn
        conn.execute(f"CREATE TEMP TABLE IF NOT EXISTS {tmp}({', '.join(c + ' TEXT' for c in cols)})")
        conn.execute(f"DELETE FROM {tmp}")
        if keys:
            placeholders = ",".join("?" * len(cols))
            conn.executemany(
                f"INSERT OR IGNORE INTO {tmp} VALUES({placeholders})",
                [tuple(k) if isinstance(k, tuple) else (k,) for k in keys],
            )
            match = " AND ".join(f"{table}.{c} = {tmp}.{c}" for c in cols)
            conn.execute(f"DELETE FROM {table} WHERE NOT EXISTS (SELECT 1 FROM {tmp} WHERE {match})")
        else:
            conn.execute(f"DELETE FROM {table}")
        conn.execute(f"DELETE FROM {tmp}")
        return conn.total_changes

    def _take_snapshot(self, rev: int) -> None:
        """每 N 次提交留一份完整快照，并丢掉过老的。

        时间旅行的精度由 N 决定：N=50 意味着「最多回退 50 次提交」。
        这是刻意的取舍 —— 每次提交都存全量，等于把刚省下的写放大又加回来。
        """
        conn = self.conn
        nodes = [json.loads(r[0]) for r in conn.execute("SELECT json FROM nodes")]
        edges = [[a, b, json.loads(j)] for a, b, j in conn.execute("SELECT a,b,json FROM edges")]
        blobs = {k: json.loads(v) for k, v in conn.execute("SELECT key,json FROM blobs")}
        tombstones = [json.loads(r[0]) for r in conn.execute("SELECT json FROM tombstones")]
        payload = {
            "version": blobs.get("config") and int(self._meta("payload_version", "0") or 0),
            **{k: blobs[k] for k in BLOB_KEYS if k in blobs},
            "graph": {"nodes": nodes, "edges": edges},
            "events": [],
            "tombstone": tombstones,
            "last_report": blobs.get("last_report"),
        }
        conn.execute(
            "INSERT OR REPLACE INTO snapshots(rev, ts, json) VALUES(?,?,?)",
            (rev, time.time(), json.dumps(payload, ensure_ascii=False)),
        )
        conn.execute(
            "DELETE FROM snapshots WHERE rev NOT IN "
            "(SELECT rev FROM snapshots ORDER BY rev DESC LIMIT ?)",
            (self.snapshot_keep,),
        )

    # ---- 运维 ----

    def history(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT rev, ts, reason, writer, nodes_written, nodes_total, bytes, digest "
            "FROM commits ORDER BY rev DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [
            {
                "rev": r[0],
                "ts": r[1],
                "reason": r[2],
                "writer": r[3],
                "nodes_written": r[4],
                "nodes_total": r[5],
                "bytes": r[6],
                "digest": r[7],
            }
            for r in rows
        ]

    def state_at(self, revision: int) -> dict[str, Any] | None:
        """取 ≤ revision 的最近一份完整快照。没有就诚实返回 None。"""
        row = self.conn.execute(
            "SELECT json FROM snapshots WHERE rev <= ? ORDER BY rev DESC LIMIT 1", (int(revision),)
        ).fetchone()
        if row is None:
            return None
        return json.loads(row[0])

    def stats_report(self) -> dict[str, Any]:
        conn = self.conn
        one = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
        return {
            "backend": "sqlite",
            "path": self.path,
            "bytes": os.path.getsize(self.path) if os.path.exists(self.path) else 0,
            "journal_mode": one("PRAGMA journal_mode"),
            "synchronous": one("PRAGMA synchronous"),
            "nodes": one("SELECT COUNT(*) FROM nodes"),
            "edges": one("SELECT COUNT(*) FROM edges"),
            "tombstones": one("SELECT COUNT(*) FROM tombstones"),
            "events": one("SELECT COUNT(*) FROM events"),
            "commits": one("SELECT COUNT(*) FROM commits"),
            "snapshots": one("SELECT COUNT(*) FROM snapshots"),
            "revision": self.revision,
            "state_id": self.state_id,
            "counters": dict(self.stats),
        }

    def fsck(self) -> dict[str, Any]:
        conn = self.conn
        report: dict[str, Any] = {"backend": "sqlite", "path": self.path}
        try:
            report["integrity"] = conn.execute("PRAGMA integrity_check").fetchone()[0]
        except sqlite3.DatabaseError as exc:
            report["integrity"] = f"error: {exc}"
        try:
            report.update(
                {
                    "journal_mode": conn.execute("PRAGMA journal_mode").fetchone()[0],
                    "nodes": conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0],
                    "edges": conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0],
                    "events": conn.execute("SELECT COUNT(*) FROM events").fetchone()[0],
                    "commits": conn.execute("SELECT COUNT(*) FROM commits").fetchone()[0],
                    "revision": int(self._meta("revision", "0") or 0),
                    "max_commit": conn.execute("SELECT COALESCE(MAX(rev),0) FROM commits").fetchone()[0],
                }
            )
            report["orphan_edges"] = conn.execute(
                "SELECT COUNT(*) FROM edges e WHERE NOT EXISTS(SELECT 1 FROM nodes n WHERE n.name=e.a)"
                " OR NOT EXISTS(SELECT 1 FROM nodes n WHERE n.name=e.b)"
            ).fetchone()[0]
            report["lock"] = holder_of(self.path + ".lock")
            report["ok"] = (
                str(report.get("integrity")) == "ok"
                and report["orphan_edges"] == 0
                and report["revision"] == report["max_commit"]
            )
        except sqlite3.DatabaseError as exc:
            report["ok"] = False
            report["error"] = str(exc)
        if os.path.exists(self.path + "-wal"):
            report["wal_bytes"] = os.path.getsize(self.path + "-wal")
        return report

    def repair(self) -> dict[str, Any]:
        """修可修的那部分：悬空边、meta 落后、孤儿 WAL。**不动节点内容。**"""
        conn = self.conn
        backup = _backup_before(self.path)
        self.begin("repair")
        try:
            dropped = conn.execute(
                "DELETE FROM edges WHERE NOT EXISTS(SELECT 1 FROM nodes n WHERE n.name=edges.a)"
                " OR NOT EXISTS(SELECT 1 FROM nodes n WHERE n.name=edges.b)"
            ).rowcount
            max_rev = conn.execute("SELECT COALESCE(MAX(rev),0) FROM commits").fetchone()[0]
            fixed = False
            if int(self._meta("revision", "0") or 0) != int(max_rev):
                self._set_meta("revision", int(max_rev))
                fixed = True
            self.commit()
        except BaseException:
            self.rollback()
            raise
        self.revision = int(self._meta("revision", "0") or 0)
        return {"ok": True, "backup": backup, "dropped_edges": dropped, "meta_fixed": fixed}

    def export(self, dest: str, include_journal: bool = True) -> dict[str, Any]:
        result = self.load()
        return _write_bundle(
            dest,
            result.payload,
            revision=result.revision,
            state_id=result.state_id,
            source=self.path,
            history=self.history(limit=200) if include_journal else [],
        )

    def import_(self, src: str) -> dict[str, Any]:
        payload, meta = _read_bundle(src)
        if self.validate:
            self.validate(payload)
        backup = _backup_before(self.path)
        self.state_id = str(meta.get("state_id") or uuid.uuid4())
        # 不清空缓存：清空等于「假装库里什么都没有」，重复导入会把每一行都重写一遍。
        # 缓存是 name→json 的映射，导入的内容若真的不同，比对自然会判定为需要写。
        result = self.save(payload, reason="import", delta=False)
        return {
            "ok": True,
            "revision": result.revision,
            "bytes": result.bytes,
            "backup": backup,
            "durable": result.durable,
        }

    def prune_events(self, keep: int = 20000) -> dict[str, Any]:
        """库里的 events 只增不减是特性，但不能无限长：给一个显式的裁剪动作。"""
        conn = self.conn
        self.begin("prune")
        try:
            total = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            if total > keep:
                conn.execute(
                    "DELETE FROM events WHERE seq NOT IN "
                    "(SELECT seq FROM events ORDER BY seq DESC LIMIT ?)",
                    (int(keep),),
                )
            self.commit()
        except BaseException:
            self.rollback()
            raise
        return {"ok": True, "removed": max(0, int(total) - int(keep)), "keep": keep}

    def describe(self) -> dict[str, Any]:
        return {
            "backend": "sqlite",
            "path": self.path,
            "revision": self.revision,
            "state_id": self.state_id,
            "stats": dict(self.stats),
            "supports_delta": True,
            "snapshot_every": self.snapshot_every,
        }

    def close(self) -> None:
        if self._conn is not None:
            try:
                if self._in_txn:
                    self.rollback()
                self._conn.close()
            finally:
                self._conn = None

    def __del__(self) -> None:  # pragma: no cover - 兜底：忘了 close 也别把文件锁带走
        try:
            self.close()
        except Exception:
            pass


def _host() -> str:
    import socket

    return socket.gethostname()
