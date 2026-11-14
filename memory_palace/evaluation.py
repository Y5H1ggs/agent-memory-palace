"""evaluation.py - 组件级评测集：把「我觉得它变好了」换成「这些指标说它变好了」。

为什么必须有这个文件：这个项目以前的测试全是**我写的断言**。断言通过只证明
「实现符合我的预期」，不证明「检索真的好用」。两者之间隔着一个东西：
**带标注的数据**。没有它，调参就是看着几个例子手感微调，换一批语料就漂。

这里放四类带标注的数据，覆盖四个会独立出错的组件：

    retrieval    给定查询，应该召回哪些记忆（recall@k / MRR / nDCG）
    abstain      哪些查询**本就不该**召回任何东西（弃答的准确率与误弃率）
    dedupe       两句话该不该合并（阈值扫描 + 最优 F1）
    consolidation 哪些碎片应该聚成同一条高层记忆（成对 F1）

两个领域（A 股复盘 / 工程运维）是刻意的：单领域的数据集会让任何阈值都显得很好，
换领域一测就露馅 —— 这就是「阈值换个语料就漂」的定量版本。

`kind` 字段标注查询类型：
    exact       查询词与记忆高度重叠（词法通道应该赢）
    paraphrase  换了个说法（只有真正的语义模型才能赢）
    unanswerable 库里没有答案（应该弃答）

指标不美化：如果 paraphrase 的召回只有 0.2，报告里就写 0.2，
并明确说「内置哈希嵌入不解决同义替换，要解决请接真模型」。
一个诚实的 0.2 比一个好看的 0.9 有用 —— 后者只会让人在错误的地方加码。
"""
from __future__ import annotations

import math
import statistics
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .core import MemoryGraph, MemoryNode, MutableClock, PalaceConfig
from .semantics import SemanticIndex


# ---------------------------------------------------------------- 数据模型


@dataclass
class Query:
    text: str
    gold: tuple[str, ...]
    kind: str = "exact"


@dataclass
class Dataset:
    name: str
    memories: list[tuple[str, str, tuple[str, ...]]]
    queries: list[Query]
    dedupe_pairs: list[tuple[str, str, bool]] = field(default_factory=list)
    clusters: list[list[str]] = field(default_factory=list)

    def graph(self) -> MemoryGraph:
        g = MemoryGraph()
        for name, content, tags in self.memories:
            g.add_node(
                MemoryNode(
                    name=name,
                    content=content,
                    tags=tuple(tags),
                    tier="fact",
                    created_at=1_800_000_000.0,
                    last_accessed=1_800_000_000.0,
                )
            )
        return g


# ---------------------------------------------------------------- 数据集：A 股复盘


FINANCE = Dataset(
    name="zh-finance",
    memories=[
        ("pos:kelly", "单笔仓位按半凯利给，最多不超过总资金的三分之一", ("仓位", "凯利")),
        ("pos:spread", "同时最多持三只，分散到不同板块，不做单只满仓", ("仓位", "分散")),
        ("pos:add", "亏损单不加仓，只在盈利仓位上做金字塔加仓", ("仓位", "加仓")),
        ("pos:cost", "双边交易成本约占本金千分之二，单次收益要跑赢它才算真赚", ("成本",)),
        ("risk:stop", "跌破买入价的百分之五无条件止损，不找理由", ("风险", "止损")),
        ("risk:drawdown", "账户回撤超过百分之十就降到半仓，等情绪修复再加回来", ("风险", "回撤")),
        ("risk:noavg", "禁止对亏损标的摊平成本，摊平是把错误放大", ("风险", "禁止")),
        ("risk:event", "重大事件前减仓，不确定的时候不赌方向", ("风险", "事件")),
        ("sector:relay", "板块内资金接力优先看连板基因，首板后第二天看资金选谁", ("板块", "资金接力")),
        ("sector:main", "只做主线题材，支线小题材不参与，主线之外没有超额", ("板块", "主线")),
        ("sector:leader", "龙头股换手率超过百分之二十要警惕分歧转一致失败", ("板块", "龙头")),
        ("sector:first", "低位首板优先于高位连板，高位板接力风险收益比差", ("板块", "首板")),
        ("sector:auction", "竞价高开超过百分之五但量能不足，大概率是诱多", ("板块", "竞价")),
        ("review:daily", "每天收盘后复盘当日交易，写下买卖理由与当时的情绪", ("复盘",)),
        ("review:mistake", "复盘只看两件事：有没有按纪律执行，错在哪里", ("复盘", "纪律")),
        ("review:weekly", "每周统计胜率与盈亏比，连续两周低于四成胜率就停手", ("复盘", "统计")),
        ("review:log", "交易记录必须包含买入理由、止损位、目标位三个字段", ("复盘", "记录")),
        ("rule:noabs", "禁止使用绝对化措辞，所有判断必须带概率", ("硬规则", "措辞")),
        ("rule:perm", "没有创业板与科创板交易权限，只做主板六十与零零开头", ("硬规则", "权限")),
        ("rule:three", "同时持仓不超过三只，超过就说明在研究上偷懒了", ("硬规则", "仓位")),
        ("style:table", "输出偏好表格加分点，拒绝大段无分段长文本", ("输出", "表格")),
        ("style:short", "回答要短，先给结论再给依据，不要铺垫", ("输出", "精简")),
        ("style:risk", "每次给建议末尾必须附风险提示，说明不构成投资建议", ("输出", "风险")),
        ("style:prob", "所有情景必须标概率，按概率排序并标出高概率事件", ("输出", "概率")),
        ("macro:rate", "美债收益率快速上行时，成长股估值先杀，先减仓再观察", ("宏观", "利率")),
        ("macro:cpi", "CPI 超预期时关注消费与农业，回避高估值成长", ("宏观", "通胀")),
        ("macro:fx", "人民币快速贬值利好出口链，利空航空与造纸", ("宏观", "汇率")),
        ("flow:north", "北向资金连续三天净流出，降低仓位不加仓", ("资金", "北向")),
        ("flow:volume", "成交量萎缩到近期均量的六成以下，说明分歧消失，等待方向", ("资金", "量能")),
        ("flow:margin", "两融余额快速上升是情绪过热的信号，不是买入信号", ("资金", "两融")),
        ("psych:fomo", "看到别人赚钱就想追，这是 FOMO，先等一天再决定", ("心理", "追高")),
        ("psych:loss", "亏损后想立刻扳回来是最危险的情绪，当天不再开新仓", ("心理", "亏损")),
        ("psych:over", "连续盈利三笔后降低仓位，过度自信比亏损更贵", ("心理", "过度自信")),
        ("ops:backup", "记忆库每天备份，备份文件保留七天，放在另一个磁盘", ("运维", "备份")),
        ("ops:monitor", "服务响应时间超过五百毫秒要告警，先看磁盘再看锁", ("运维", "监控")),
        ("data:source", "行情数据只用交易所与东财两个来源，交叉验证后再用", ("数据", "来源")),
        ("data:adjust", "复权方式统一用前复权，跨周期比较才不会失真", ("数据", "复权")),
        ("theory:expect", "期望值等于胜率乘盈利减败率乘亏损，正期望才下注", ("理论", "期望")),
        ("theory:ruin", "任何单笔都不能造成无法恢复的损失，先算破产概率", ("理论", "破产")),
    ],
    queries=[
        Query("单笔最多投多少钱", ("pos:kelly",), "paraphrase"),
        Query("一次买入占多少资金比例", ("pos:kelly",), "paraphrase"),
        Query("同时持几只股票比较合适", ("pos:spread", "rule:three"), "paraphrase"),
        Query("亏了要不要补仓摊低成本", ("risk:noavg", "pos:add"), "paraphrase"),
        Query("跌多少必须卖", ("risk:stop",), "paraphrase"),
        Query("账户亏了百分之十几怎么办", ("risk:drawdown",), "paraphrase"),
        Query("手续费大概多少", ("pos:cost",), "paraphrase"),
        Query("怎么判断板块里谁会接力", ("sector:relay",), "exact"),
        Query("首板和高位连板选哪个", ("sector:first",), "exact"),
        Query("竞价高开很多能不能追", ("sector:auction",), "exact"),
        Query("每天收盘后要做什么", ("review:daily",), "paraphrase"),
        Query("胜率低到多少该休息", ("review:weekly",), "paraphrase"),
        Query("能不能买三百开头的股票", ("rule:perm",), "exact"),
        Query("回答格式有什么要求", ("style:table", "style:short"), "paraphrase"),
        Query("美债收益率上行怎么应对", ("macro:rate",), "exact"),
        Query("北向资金连续流出怎么办", ("flow:north",), "exact"),
        Query("成交量萎缩说明什么", ("flow:volume",), "exact"),
        Query("连续赚钱之后要注意什么", ("psych:over",), "paraphrase"),
        Query("记忆库怎么备份", ("ops:backup",), "exact"),
        Query("期望值怎么算", ("theory:expect",), "exact"),
        Query("今天天气怎么样", (), "unanswerable"),
        Query("帮我写一首关于春天的诗", (), "unanswerable"),
        Query("量子计算的退相干时间是多少", (), "unanswerable"),
        Query("怎么给新生儿办理护照", (), "unanswerable"),
    ],
    dedupe_pairs=[
        ("单笔仓位按半凯利给，最多不超过总资金的三分之一", "单笔仓位按半凯利给，最多不超过总资金的三分之一。", True),
        ("跌破买入价的百分之五无条件止损", "跌破买入价 5% 就无条件止损，不找理由", True),
        ("单笔不超过总资金三分之一", "单笔不超过总资金三分之一，且最多同时持三只", False),
        ("禁止对亏损标的摊平成本", "禁止对亏损标的摊平成本，摊平是把错误放大", False),
        ("输出偏好表格加分点", "输出偏好表格加分点，拒绝大段长文本", False),
        ("只做主线题材", "只做主线题材，支线不参与", False),
        ("账户回撤超过百分之十降到半仓", "账户回撤超过百分之十就降到半仓，等情绪修复再加回来", False),
        ("回答要短，先给结论", "回答要短，先给结论再给依据", False),
        ("每天收盘后复盘", "每天收盘后复盘当日交易", False),
        ("没有创业板权限只做主板", "没有创业板与科创板交易权限，只做主板六十与零零开头", False),
    ],
    clusters=[
        ["pos:kelly", "pos:spread", "rule:three"],
        ["risk:stop", "risk:drawdown", "risk:noavg"],
        ["sector:relay", "sector:first", "sector:auction"],
        ["review:daily", "review:mistake", "review:weekly"],
        ["style:table", "style:short", "style:prob"],
    ],
)


# ---------------------------------------------------------------- 数据集：工程运维


TECH = Dataset(
    name="zh-tech",
    memories=[
        ("db:wal", "SQLite 开 WAL 模式，写者不阻塞读者，busy_timeout 设十秒", ("数据库", "sqlite")),
        ("db:index", "查询慢先看执行计划，全表扫就加索引，索引列放等值条件在前", ("数据库", "索引")),
        ("db:txn", "写事务用 BEGIN IMMEDIATE，一开头就声明写意图，避免锁升级死锁", ("数据库", "事务")),
        ("db:backup", "备份用 VACUUM INTO 生成一致性快照，不要直接复制正在写的库文件", ("数据库", "备份")),
        ("db:migrate", "建表用 CREATE TABLE IF NOT EXISTS，字段变更走版本化迁移脚本", ("数据库", "迁移")),
        ("svc:timeout", "所有外部调用必须设超时，没有超时的客户端等于没有错误处理", ("服务", "超时")),
        ("svc:retry", "重试必须带指数退避与抖动，且只重试幂等操作", ("服务", "重试")),
        ("svc:circuit", "依赖连续失败五次就熔断，降级到缓存，别把故障放大到全站", ("服务", "熔断")),
        ("svc:health", "健康检查要区分存活与就绪，就绪检查必须探测依赖", ("服务", "健康检查")),
        ("svc:limit", "入口做限流，按用户与按接口两个维度都要有", ("服务", "限流")),
        ("log:struct", "日志用结构化 JSON，包含 trace_id，禁止打印完整身份证与手机号", ("日志", "脱敏")),
        ("log:level", "错误日志带堆栈，信息日志不打堆栈，日志级别要能热调", ("日志", "级别")),
        ("log:rotate", "日志按天轮转保留十四天，单文件超过两百兆就切", ("日志", "轮转")),
        ("obs:trace", "跨服务调用传递 trace 上下文，一个请求一条链路能串起来", ("可观测", "链路")),
        ("obs:metric", "四个黄金指标：延迟、流量、错误率、饱和度", ("可观测", "指标")),
        ("obs:alert", "告警必须可执行，连续三次无人处理就降级为工单", ("可观测", "告警")),
        ("k8s:probe", "liveness 探针不要检查外部依赖，否则依赖抖动会引发全量重启", ("kubernetes", "探针")),
        ("k8s:resource", "所有容器都要设 requests 与 limits，没有 limits 的容器会拖垮节点", ("kubernetes", "资源")),
        ("k8s:pdb", "给关键服务配 PodDisruptionBudget，滚动升级才不至于全下线", ("kubernetes", "预算")),
        ("ci:cache", "构建缓存按锁文件哈希做键，命中率能到八成以上", ("ci", "缓存")),
        ("ci:matrix", "多版本测试用矩阵，至少覆盖最低支持版本与最新版本", ("ci", "矩阵")),
        ("ci:flaky", "偶发失败的测试要隔离并记录，不要直接重跑掩盖它", ("ci", "偶发")),
        ("py:gIL", "CPU 密集用多进程，IO 密集用多线程或协程，别拿线程跑计算", ("python", "并发")),
        ("py:typing", "公共接口写类型标注，用 mypy 严格模式挡住 None 漏判", ("python", "类型")),
        ("py:dep", "依赖锁版本并定期审计，零依赖的库升级最省心", ("python", "依赖")),
        ("sec:secret", "密钥只从环境变量或密钥管理服务读，绝不进代码库", ("安全", "密钥")),
        ("sec:input", "所有外部输入都要校验与转义，SQL 一律用参数化查询", ("安全", "输入")),
        ("sec:least", "服务账号按最小权限授权，能只读就别给写", ("安全", "权限")),
        ("perf:profile", "优化前先采样，没有火焰图的优化都是猜", ("性能", "采样")),
        ("perf:nplus", "ORM 的 N+1 查询是接口变慢的头号原因，先看 SQL 条数", ("性能", "orm")),
        ("perf:cache", "缓存要设过期与容量上限，没有淘汰策略的缓存是内存泄漏", ("性能", "缓存")),
        ("ops:runbook", "每个告警都要有处置手册，包含判断依据与回滚步骤", ("运维", "手册")),
        ("ops:rollback", "发布必须可回滚，回滚脚本与发布脚本一起评审", ("运维", "回滚")),
        ("ops:postmortem", "故障复盘只谈事实与改进项，不追责个人", ("运维", "复盘")),
        ("git:commit", "提交信息写清动机与影响面，一个提交只做一件事", ("git", "提交")),
        ("git:branch", "主干保持可发布，功能在分支上开发，合并前必须过 CI", ("git", "分支")),
        ("docs:adr", "重要技术决策写 ADR，记录当时的约束与被否掉的方案", ("文档", "决策")),
        ("test:pyramid", "测试金字塔：多写单元测试，少写端到端，端到端只覆盖主路径", ("测试", "分层")),
        ("test:fixture", "测试用固定时钟与临时目录，不依赖真实网络与当前时间", ("测试", "夹具")),
        ("test:assert", "断言要能失败：写完先故意改坏实现，确认测试真的会红", ("测试", "有效性")),
    ],
    queries=[
        Query("怎么让读写不互相阻塞", ("db:wal",), "paraphrase"),
        Query("sqlite 写事务怎么避免死锁", ("db:txn",), "exact"),
        Query("查询很慢从哪里查起", ("db:index",), "paraphrase"),
        Query("怎么备份正在使用的数据库", ("db:backup",), "paraphrase"),
        Query("外部调用老是卡住", ("svc:timeout",), "paraphrase"),
        Query("重试要怎么做才安全", ("svc:retry",), "exact"),
        Query("依赖挂了怎么防止全站雪崩", ("svc:circuit",), "paraphrase"),
        Query("日志里不能出现什么", ("log:struct",), "paraphrase"),
        Query("怎么定位一个请求经过了哪些服务", ("obs:trace",), "paraphrase"),
        Query("liveness 探针要不要检查数据库", ("k8s:probe",), "exact"),
        Query("容器不设 limits 会怎样", ("k8s:resource",), "exact"),
        Query("构建缓存怎么配", ("ci:cache",), "exact"),
        Query("python 里计算密集该怎么并行", ("py:gIL",), "paraphrase"),
        Query("密钥应该放在哪里", ("sec:secret",), "paraphrase"),
        Query("接口变慢最常见的原因", ("perf:nplus",), "paraphrase"),
        Query("发布出问题怎么退回去", ("ops:rollback",), "paraphrase"),
        Query("怎么写测试才算有效", ("test:assert",), "paraphrase"),
        Query("技术决策要记录什么", ("docs:adr",), "paraphrase"),
        Query("如何给猫剪指甲", (), "unanswerable"),
        Query("推荐一部科幻电影", (), "unanswerable"),
        Query("红烧肉要炖多久", (), "unanswerable"),
        Query("股票印花税是多少", (), "unanswerable"),
    ],
    dedupe_pairs=[
        ("SQLite 开 WAL 模式，写者不阻塞读者", "SQLite 开 WAL 模式，写者不阻塞读者。", True),
        ("所有外部调用必须设超时", "所有外部调用必须设超时，没有超时的客户端等于没有错误处理", False),
        ("日志用结构化 JSON", "日志用结构化 JSON，包含 trace_id", False),
        ("缓存要设过期与容量上限", "缓存要设过期与容量上限，没有淘汰策略的缓存是内存泄漏", False),
        ("发布必须可回滚", "发布必须可回滚，回滚脚本与发布脚本一起评审", False),
    ],
    clusters=[
        ["db:wal", "db:txn", "db:index"],
        ["svc:timeout", "svc:retry", "svc:circuit"],
        ["log:struct", "log:level", "log:rotate"],
        ["obs:trace", "obs:metric", "obs:alert"],
        ["ci:cache", "ci:matrix", "ci:flaky"],
    ],
)


DATASETS: dict[str, Dataset] = {FINANCE.name: FINANCE, TECH.name: TECH}


# ---------------------------------------------------------------- 指标


def recall_at_k(ranked: Sequence[str], gold: Iterable[str], k: int) -> float:
    golds = set(gold)
    if not golds:
        return 0.0
    hits = len(golds & set(ranked[:k]))
    return hits / len(golds)


def mrr(ranked: Sequence[str], gold: Iterable[str]) -> float:
    golds = set(gold)
    for i, name in enumerate(ranked, 1):
        if name in golds:
            return 1.0 / i
    return 0.0


def ndcg_at_k(ranked: Sequence[str], gold: Iterable[str], k: int) -> float:
    golds = set(gold)
    if not golds:
        return 0.0
    dcg = sum(
        1.0 / math.log2(i + 2) for i, name in enumerate(ranked[:k]) if name in golds
    )
    ideal = sum(1.0 / math.log2(i + 2) for i in range(min(len(golds), k)))
    return dcg / ideal if ideal else 0.0


@dataclass
class Metrics:
    n: int = 0
    recall: float = 0.0
    mrr: float = 0.0
    ndcg: float = 0.0
    precision1: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "recall@k": round(self.recall, 4),
            "mrr": round(self.mrr, 4),
            "ndcg@k": round(self.ndcg, 4),
            "p@1": round(self.precision1, 4),
        }


def _mean(values: Sequence[float]) -> float:
    return float(statistics.fmean(values)) if values else 0.0


def retrieval_metrics(ranked_lists: Sequence[Sequence[str]], queries: Sequence[Query], k: int) -> Metrics:
    recalls, mrrs, ndcgs, p1 = [], [], [], []
    for ranked, q in zip(ranked_lists, queries):
        recalls.append(recall_at_k(ranked, q.gold, k))
        mrrs.append(mrr(ranked, q.gold))
        ndcgs.append(ndcg_at_k(ranked, q.gold, k))
        p1.append(1.0 if ranked and ranked[0] in set(q.gold) else 0.0)
    return Metrics(len(queries), _mean(recalls), _mean(mrrs), _mean(ndcgs), _mean(p1))


# ---------------------------------------------------------------- 运行器


def _clock() -> MutableClock:
    return MutableClock(1_800_000_000.0)


def build_index(dataset: Dataset, embedder: Any = None, config: PalaceConfig | None = None) -> SemanticIndex:
    """只建语义索引：评测的是「检索组件」，不是整条 palace 流水线。"""
    cfg = config or PalaceConfig()
    index = SemanticIndex(config=cfg, embedder=embedder)
    index.rebuild(dataset.graph())
    return index


def run_retrieval(
    dataset: Dataset,
    embedder: Any = None,
    config: PalaceConfig | None = None,
    k: int = 5,
    channel: str = "hybrid",
) -> dict[str, Any]:
    index = build_index(dataset, embedder=embedder, config=config)
    answerable = [q for q in dataset.queries if q.kind != "unanswerable"]
    ranks: list[list[str]] = []
    by_kind: dict[str, list[list[str]]] = {"exact": [], "paraphrase": []}
    for q in answerable:
        if channel == "lexical":
            scored = index.lexical_scores(q.text)
        elif channel == "dense":
            scored = index.dense_scores(q.text)
        else:
            scored = index.scores(q.text)
        ranked = [n for n, _ in sorted(scored.items(), key=lambda kv: (-kv[1], kv[0]))][:k]
        ranks.append(ranked)
        by_kind.setdefault(q.kind, []).append(ranked)
    report: dict[str, Any] = {
        "dataset": dataset.name,
        "channel": channel,
        "overall": retrieval_metrics(ranks, answerable, k).to_dict(),
    }
    for kind, lists in by_kind.items():
        if lists:
            qs = [q for q in answerable if q.kind == kind]
            report[kind] = retrieval_metrics(lists, qs, k).to_dict()
    return report


def run_abstain(
    dataset: Dataset,
    threshold: float | None = None,
    config: PalaceConfig | None = None,
    embedder: Any = None,
) -> dict[str, Any]:
    """弃答 = 「最高分低于阈值就返回空」。分别统计两类错误，别只报准确率。

    不传阈值就用**当前配置的默认值** —— 评估必须测线上真正在跑的那条线，
    拿一个写死的旧常数去评估，测的是另一个系统。
    """
    cfg = config or PalaceConfig()
    if threshold is None:
        threshold = cfg.abstain_semantic_floor
    index = build_index(dataset, embedder=embedder, config=cfg)
    tp = fp = tn = fn = 0
    for q in dataset.queries:
        scored = index.scores(q.text)
        top = max(scored.values()) if scored else 0.0
        abstained = top < threshold
        if q.kind == "unanswerable":
            tp += int(abstained)
            fn += int(not abstained)
        else:
            fp += int(abstained)
            tn += int(not abstained)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "dataset": dataset.name,
        "threshold": threshold,
        "abstain_precision": round(precision, 4),
        "abstain_recall": round(recall, 4),
        "abstain_f1": round(f1, 4),
        "false_abstain": fp,
        "missed_abstain": fn,
        "counts": {"tp": tp, "fp": fp, "tn": tn, "fn": fn},
    }


def run_dedupe(
    dataset: Dataset,
    threshold: float | None = None,
    length_ratio: float | None = None,
    config: PalaceConfig | None = None,
    embedder: Any = None,
) -> dict[str, Any]:
    cfg = config or PalaceConfig()
    if threshold is None:
        threshold = cfg.dedupe_similarity
    if length_ratio is None:
        length_ratio = cfg.dedupe_length_ratio
    index = build_index(dataset, embedder=embedder, config=cfg)
    tp = fp = tn = fn = 0
    for a, b, same in dataset.dedupe_pairs:
        score = index.pair_score(a, b)
        ratio = min(len(a), len(b)) / max(len(a), len(b))
        predicted = score >= threshold and ratio >= length_ratio
        if same:
            tp += int(predicted)
            fn += int(not predicted)
        else:
            fp += int(predicted)
            tn += int(not predicted)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "dataset": dataset.name,
        "threshold": threshold,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "counts": {"tp": tp, "fp": fp, "tn": tn, "fn": fn},
    }


def sweep_dedupe(dataset: Dataset, config: PalaceConfig | None = None) -> dict[str, Any]:
    curve = []
    for step in range(70, 100):
        t = step / 100
        r = run_dedupe(dataset, threshold=t, config=config)
        curve.append({"threshold": t, **{k: r[k] for k in ("precision", "recall", "f1")}})
    best = max(curve, key=lambda row: (row["f1"], row["threshold"]))
    return {"best": best, "curve": curve}


def run_consolidation(
    dataset: Dataset, threshold: float | None = None, config: PalaceConfig | None = None
) -> dict[str, Any]:
    """成对 F1：同一金标簇内的两两配对算真阳，其余算真阴。

    测的是**真实沉淀引擎**（标签同源 + 共现 + 余弦取较大者），
    不是「拿两段话算个余弦」—— 后者既低估了它（标签通道没用上），
    也高估了它（没有连通分量的连带效应），测出来的数不代表线上行为。
    """
    if not dataset.clusters:
        return {"dataset": dataset.name, "skipped": True}
    from .consolidation import ConsolidationEngine

    cfg = PalaceConfig(
        consolidate_similarity=threshold if threshold is not None else 0.5,
        consolidate_min_group=2,
        consolidate_min_shared_tags=1,
        consolidate_min_weight=0.0,
    )
    graph = dataset.graph()
    index = SemanticIndex(config=cfg)
    index.rebuild(graph)
    engine = ConsolidationEngine(
        cfg, None, vector_lookup=lambda node: index.encode_query(node.content)
    )
    candidates = list(graph.all_nodes())
    groups = [g for g in engine._clusters(candidates, graph) if len(g) > 1]

    member_names = {n for c in dataset.clusters for n in c}
    truth: set[tuple[str, str]] = set()
    for cluster in dataset.clusters:
        names = sorted(cluster)
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                truth.add((names[i], names[j]))
    predicted: set[tuple[str, str]] = set()
    for group in groups:
        names = sorted(n.name for n in group if n.name in member_names)
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                predicted.add((names[i], names[j]))
    tp = len(truth & predicted)
    fp = len(predicted - truth)
    fn = len(truth - predicted)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "dataset": dataset.name,
        "threshold": cfg.consolidate_similarity,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "groups": len(groups),
    }
    tp = len(truth & predicted)
    fp = len(predicted - truth)
    fn = len(truth - predicted)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "dataset": dataset.name,
        "threshold": threshold,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
    }


def sweep_consolidation(dataset: Dataset) -> dict[str, Any]:
    """沉淀阈值扫描：默认 0.5 在「标签不共享」的语料上几乎不出簇，这一扫就能看见。"""
    curve = []
    for step in range(15, 61, 5):
        t = step / 100
        r = run_consolidation(dataset, threshold=t)
        curve.append({"threshold": t, "precision": r["precision"], "recall": r["recall"], "f1": r["f1"]})
    best = max(curve, key=lambda row: (row["f1"], row["threshold"]))
    return {"best": best, "curve": curve}


def run_suite(
    names: Sequence[str] | None = None,
    *,
    k: int = 5,
    embedder: Any = None,
    config: PalaceConfig | None = None,
    chunk: str = "lexical",
) -> dict[str, Any]:
    """跑一遍全部组件。`chunk` 是主力通道：lexical / hybrid / dense。"""
    chosen = [DATASETS[n] for n in (names or DATASETS)]
    started = time.perf_counter()
    report: dict[str, Any] = {
        "suite": "memory-palace/components",
        "k": k,
        "channel": chunk,
        "datasets": [],
    }
    for ds in chosen:
        entry = {
            "dataset": ds.name,
            "retrieval": run_retrieval(ds, embedder=embedder, config=config, k=k, channel=chunk),
            "abstain": run_abstain(ds, config=config, embedder=embedder),
            "dedupe": run_dedupe(ds, config=config, embedder=embedder),
            "consolidation": run_consolidation(ds, config=config),
            "consolidation_swept": sweep_consolidation(ds),
        }
        report["datasets"].append(entry)
    report["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return report


def format_report(report: dict[str, Any]) -> str:
    """人读版：每个数据集一张小表，拒绝把数字埋在 JSON 里。"""
    lines = [
        f"通道 {report['channel']} · k={report['k']} · 用时 {report['elapsed_ms']}ms",
        "",
    ]
    for entry in report["datasets"]:
        ret = entry["retrieval"]
        lines.append(f"[{entry['dataset']}] 检索")
        lines.append("  子集        n    recall   mrr    ndcg   p@1")
        for key in ("overall", "exact", "paraphrase"):
            if key in ret:
                m = ret[key]
                lines.append(
                    f"  {key:<10} {m['n']:>3}   {m['recall@k']:<7} {m['mrr']:<6} "
                    f"{m['ndcg@k']:<6} {m['p@1']}"
                )
        ab = entry["abstain"]
        lines.append(
            f"  弃答 F1={ab['abstain_f1']} 误弃={ab['false_abstain']} "
            f"漏弃={ab['missed_abstain']}（阈值 {ab['threshold']}）"
        )
        dd = entry["dedupe"]
        lines.append(f"  去重 P={dd['precision']} R={dd['recall']} F1={dd['f1']}")
        cs = entry.get("consolidation") or {}
        if not cs.get("skipped"):
            lines.append(
                f"  沉淀 P={cs['precision']} R={cs['recall']} F1={cs['f1']}"
                f"（阈值 {cs['threshold']}，出簇 {cs['groups']}）"
            )
            swept = entry.get("consolidation_swept") or {}
            if swept:
                best = swept["best"]
                lines.append(
                    f"  沉淀最优 阈值={best['threshold']} F1={best['f1']}"
                    "（默认阈值在你的语料上可能过高，用 calibrate 扫）"
                )
        lines.append("")
    return "\n".join(lines)


def calibrate(
    names: Sequence[str] | None = None,
    config: PalaceConfig | None = None,
) -> dict[str, Any]:
    """把阈值从「拍出来的」变成「在标注集上扫出来的」，并给出平台区间。

    只报一个最优点是没有用的：最优点往往是一个尖峰，旁边两步就掉一半。
    真正能用的是**平台** —— 在最优值 ±0.1 内 F1 掉不超过 2 个点的那段区间，
    取区间中点比取峰值稳。
    """
    chosen = [DATASETS[n] for n in (names or DATASETS)]
    dedupe_curve: list[dict[str, float]] = []
    abstain_curve: list[dict[str, float]] = []
    for t in [i / 100 for i in range(70, 100)]:
        f1s = [run_dedupe(ds, threshold=t, config=config)["f1"] for ds in chosen]
        dedupe_curve.append({"threshold": t, "f1": round(_mean(f1s), 4)})
    for t in [i / 100 for i in range(15, 56)]:
        f1s = [run_abstain(ds, threshold=t, config=config)["abstain_f1"] for ds in chosen]
        abstain_curve.append({"threshold": t, "f1": round(_mean(f1s), 4)})

    def plateau(curve: list[dict[str, float]]) -> dict[str, Any]:
        best = max(curve, key=lambda row: row["f1"])
        band = [row for row in curve if row["f1"] >= best["f1"] - 0.02]
        mid = (band[0]["threshold"] + band[-1]["threshold"]) / 2 if band else best["threshold"]
        return {
            "best": best,
            "plateau": [band[0]["threshold"], band[-1]["threshold"]] if band else [],
            "recommended": round(mid, 2),
        }

    return {
        "dedupe": plateau(dedupe_curve),
        "abstain": plateau(abstain_curve),
        "curves": {"dedupe": dedupe_curve, "abstain": abstain_curve},
        "datasets": [ds.name for ds in chosen],
    }


def scale_benchmark(sizes: Sequence[int] = (1000, 5000, 10000), seed: int = 7) -> list[dict[str, Any]]:
    """规模测试：把「语料上万条会退化」这句话变成一个可以看的表。

    指标只看两件：**召回有没有掉**、**单次查询延迟有没有爆**。
    """
    import random

    rng = random.Random(seed)
    topics = [
        "仓位与凯利公式的取舍", "板块资金接力与连板基因", "止损纪律与回撤控制",
        "日志结构化与链路追踪", "数据库事务与锁升级", "熔断降级与重试退避",
        "复盘记录与胜率统计", "缓存淘汰与容量上限", "测试有效性反证",
        "宏观利率与成长股估值",
    ]
    base = FINANCE.memories + TECH.memories
    out = []
    for size in sizes:
        graph = MemoryGraph()
        for i in range(size):
            name, content, tags = base[i % len(base)]
            graph.add_node(
                MemoryNode(
                    name=f"{name}#{i}",
                    content=f"{content}（第 {i} 条相关记录）",
                    tags=tuple(tags),
                    tier="fact",
                    created_at=1_800_000_000.0,
                    last_accessed=1_800_000_000.0,
                )
            )
        index = SemanticIndex(config=PalaceConfig())
        t0 = time.perf_counter()
        index.rebuild(graph)
        rebuild_ms = (time.perf_counter() - t0) * 1000
        # 用原始题目当查询，正确答案是「同名的所有副本」
        latencies = []
        hits = 0
        trials = 30
        for q in FINANCE.queries[:trials]:
            if q.kind == "unanswerable":
                continue
            t0 = time.perf_counter()
            scored = index.scores(q.text)
            latencies.append((time.perf_counter() - t0) * 1000)
            top = [n for n, _ in sorted(scored.items(), key=lambda kv: -kv[1])[:5]]
            gold = {f"{g}#{i}" for g in q.gold for i in range(size) if f"{g}#{i}" in graph}
            if gold & set(top):
                hits += 1
        out.append(
            {
                "nodes": size,
                "rebuild_ms": round(rebuild_ms, 1),
                "p50_query_ms": round(statistics.median(latencies), 2) if latencies else 0.0,
                "p95_query_ms": round(sorted(latencies)[int(len(latencies) * 0.95) - 1], 2)
                if latencies
                else 0.0,
                "recall_at_5": round(hits / max(1, len(latencies)), 3),
            }
        )
    return out
