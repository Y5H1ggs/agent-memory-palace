# 存储、并发与耐久性

> 这份文档回答三个问题：写下去的数据会不会丢、两个人同时写会不会互相覆盖、
> 出事了怎么查怎么修。每条都给出机制、代价与验证方式。

## 一、为什么换掉「整份 JSON 重写」

0.9.x 的持久化是：序列化整个库 → 写临时文件 → fsync → `os.replace`。
它保证的是一件很重要的事：**读到的一定是完整版本，不会是半截**。
它**不**保证的是：两个人各读一份、各改一处、各写一次，先写的那次改动还在。

后者不是理论问题。两个进程挂同一个 store（一个跑 `cycle`、一个在 CLI 里 `remember`）
就会发生，而且没有任何一方报错 —— 最终状态看起来完全正常，只是少了一条记忆。

同时它还有一个规模问题：改一条记忆也要重写全部内容。1000 条约 1.3 MB、47 ms；
10000 条就是 13 MB、近半秒。写延迟与库大小成正比，这在「每一步都调用」的场景里不可接受。

## 二、SQLite 后端（默认）

### 表结构

| 表 | 一行是什么 | 说明 |
|---|---|---|
| `meta` | 键值 | `schema` / `revision` / `state_id` / `payload_version` / `updated_at` |
| `nodes` | 一条记忆 | `name` 主键 + `json` + `scope`/`tier`/`kind`/`pinned`/`created_at`/`valid_to` + `updated_rev`，四个索引 |
| `edges` | 一条共现边 | `(a,b)` 主键 + `json` + `updated_rev` |
| `tombstones` | 一条墓碑 | 被显式丢弃的记忆，可 `restore` |
| `blobs` | 一个引擎状态 | `config` / `style` / `narrative` / `persona` / `selfcorrect` / `logic` / … 整体替换 |
| `events` | 一条事件 | 只追加，`(ts, kind)` 唯一索引做去重；**不随内存 trim** |
| `commits` | 一次提交 | `rev` 主键 + 时间 + 原因 + 写者 + 写入行数 + 节点总数 + 摘要 |
| `snapshots` | 一份完整快照 | 每 `snapshot_every`（默认 50）次提交留一份，只留最近 3 份 |

为什么节点要拆成行、而引擎状态仍是 blob：节点数量随使用增长（需要行级写与索引），
引擎状态是固定几个对象（整体替换更简单，也不会成为瓶颈）。这是**按数据形态**分，
不是按「哪个更时髦」分。

### 写一次发生了什么

```
BEGIN IMMEDIATE            -- 第一毫秒就声明「我是写者」
  SELECT meta.revision     -- 比内存新？先把别人的提交读进来（重载在改之前）
  INSERT/UPDATE nodes ...  -- 只写 _dirty 的那些行
  INSERT OR IGNORE events  -- 追加事件
  INSERT commits           -- 记账：这次提交是谁、为什么、写了多少
  UPDATE meta.revision     -- revision += 1
COMMIT                     -- WAL 落盘（synchronous=FULL）
```

顺序里有两处不能交换：

1. **重载在修改之前**。反过来的话，两边都认为自己最新，后写的一方把前一方
   已提交的改动整段覆盖，且双方都认为自己成功。
2. **`BEGIN IMMEDIATE` 而不是 `BEGIN`**。后者要等到第一次写才升级锁，
   两个进程可能都读完才发现要抢同一个写锁，白付一次 `SQLITE_BUSY` 重试。

### 增量写怎么判断「哪些行脏了」

`MemoryNode.__setattr__` 会把 `_dirty` 置真（不是 dataclass 字段，不进快照、
不参与相等比较）。读库之后统一清零，所以「刚打开就保存」不会重写全库（有测试守着）。
边的变化单独一个标记 `graph._edge_dirty`：共现强度提升不体现在任何单个节点上，
没有它，关联关系永远等不到落盘。

这是**唯一的**脏判定来源。宁可多写一行，不要少写一行 —— 漏写的后果是数据不一致，
多写的后果只是慢 0.1 ms。

### 代价

| | 数值 |
|---|---|
| 单次 `remember` p50 / p95（1000 条） | 1.2 ms / 1.5 ms |
| 改一条（与库大小基本无关） | 1.1 ms |
| 读库 1000 条 | 49 ms |
| 磁盘（1000 条） | 4.3 MB（行 + 索引；JSON 是 1.3 MB） |

`synchronous=FULL` 是刻意的：每个提交都 fsync。换成 `NORMAL` 能再快一点，
但「断电丢最后一次提交」对「不可再生数据」来说不是可接受的取舍。
要更快请显式改（`SqliteStore(synchronous="NORMAL")`），并知道自己放弃了什么。

## 三、JSON 后端（可移植 / 冷备）

一个库三个文件：

```
store.json          扁平快照：顶层就是 version/config/graph/events/…，附带 checksum
store.json.journal  追加日志：每条 = MAGIC + kind + len + crc32 + zlib(meta+payload)
store.json.lock     跨进程字节锁（Windows msvcrt.locking / POSIX flock）
```

写顺序：**日志先于快照**。

```
加锁 → 追加日志 + fsync → 原子替换快照 → 更新 .rev 提示
```

崩溃点在哪儿都能推出正确结论：

| 崩在哪 | 结果 |
|---|---|
| 追加日志之前 | 这次提交等于没发生（调用方也没收到成功） |
| 日志之后、快照之前 | 日志有、快照没有 → 下次启动前滚 |
| 快照之后、提示文件之前 | 快照与日志一致，提示文件旧了 → 以快照为准 |
| 日志尾部长半条 | CRC 对不上 → 丢弃它，前面的一条不少 |

`checksum` 用 sha256 覆盖整份扁平 JSON（除 `checksum` 自身），按 `sort_keys`
规范化后计算。v7 之前的老快照没有这个字段，校验按版本门控 ——
否则「正常的历史迁移」会被误判成「文件损坏」。

## 四、跨进程锁的细节

`locking.py` 提供 `FileLock`，三个实现按平台自动选：

| 平台 | 机制 |
|---|---|
| POSIX | `fcntl.flock(LOCK_EX \| LOCK_NB)` |
| Windows | `msvcrt.locking(LK_NBLCK, 1)`：锁文件第 0 个字节 |
| 兜底 | `O_CREAT \| O_EXCL` 哨兵 + 陈旧锁清理（进程已死或超 120s） |

三条容易踩的细节：

1. **持有者信息写旁路文件**（`<lock>.holder`）。Windows 的字节锁会让被锁字节在
   别的句柄下不可读写，把元信息写进锁文件在持锁期间根本做不到 —— 元信息会静默丢失，
   而它恰恰是诊断卡死时唯一有用的东西。
2. **可重入按「进程内槽位」实现**：同进程同线程重入只加计数；
   另一个线程要等 `RLock` 释放。`lock.mutex` 是 `RLock`，不是普通锁 ——
   否则同一条写路径里的嵌套 `with` 会自己把自己锁死，表现为「偶发卡住十秒」。
3. **超时是总等待时间**，不是单次尝试。生产环境宁可报错也不要无限挂住；
   超时异常里带上持有者 pid 与操作名（`LockBusy.holder`）。

## 五、多进程写同一份记忆：能到什么程度

能：多个进程各自 `remember`、互不覆盖，最后所有条目都在。

不能：**多写者高吞吐**。WAL 是「单写多读」，写者之间排队。
写热点（所有人都改同一条记忆）时，吞吐上限就是一个写者。这是 SQLite 的设计，
不是本项目的 bug。要更高写吞吐，正确做法是换服务端数据库，而不是在这上面加锁。

验证方式（每次 CI 都跑）：

```bash
python -m pytest tests/test_store.py::test_multiprocess_no_lost_updates -q
```

4 个进程 × 15 条独立写入 + 15 次 merge 同一条记忆，断言：
条目一条不少、`access_count` 恰好等于调用次数、`fsck` 通过。

## 六、出事了怎么查

```bash
memory-palace fsck              # 完整性 / 悬空边 / 账本与 meta 是否一致 / 当前锁持有者
memory-palace fsck --repair     # 修可修的：悬空边、meta 落后；先备份
memory-palace journal --limit 20
memory-palace lock              # 谁锁着，从什么时候开始，在做什么
memory-palace stats             # 行数、WAL 大小、journal_mode、synchronous
memory-palace export out.mpal   # 带 sha256 的完整档案
memory-palace import out.mpal   # 导入前自动备份现有库
```

`repair` 的边界：**不动节点内容**。它只处理结构性问题（悬空边、账本 revision 落后）。
内容层面的判断（哪条记忆该不该在）不属于工具，属于你。

## 七、已知边界

| 边界 | 影响 | 缓解 |
|---|---|---|
| 内存驻留整图 | 1e4 条约 20~40 MB；1e5 条会吃紧 | 目标规模写在 README；再大需要分片/查询下推，未做 |
| WAL 需要本地盘 | 网络盘（NFS/SMB）上共享内存不可靠 | 用本地盘；或用 JSON 后端 + 单写者 |
| 引擎状态是整体 blob | 两个进程同时跑 `cycle` 时，后提交的一方覆盖前一方对 `narrative` 等状态的修改 | 节点不受影响；派生状态按文档级 last-writer-wins，已在文档写明 |
| 快照每 50 次提交一份 | `state_at()` 只能回到最近的快照，不是任意 revision | 精确回滚请用 `export` 备份；或调小 `snapshot_every` |
