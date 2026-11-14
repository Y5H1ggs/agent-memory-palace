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


# ---------------------------------------------------------------- 数据集：用药与就医


MEDICAL = Dataset(
    name="zh-medical",
    memories=[
        ("med:allergy", "青霉素过敏史，每次就诊与取药都要主动说，病历首页要标明", ("用药", "过敏")),
        ("med:kidney", "肾功能不全，用药剂量要医生按肌酐清除率调整，不能照说明书成人量", ("用药", "肾功能")),
        ("med:interact", "就诊时带上正在服用的药清单，重复成分容易叠加过量", ("用药", "相互作用")),
        ("med:abx", "抗生素必须按疗程吃完，症状好转就停容易复发并产生耐药", ("用药", "抗生素")),
        ("med:pain", "止痛药不要空腹吃，有胃溃疡史的人用前先问医生", ("用药", "止痛")),
        ("med:herb", "中药与西药间隔两小时以上，含麻黄的中成药会影响血压", ("用药", "中药")),
        ("med:preg", "备孕与哺乳期任何药都先问医生，包括外用药与保健品", ("用药", "特殊人群")),
        ("med:bp", "家里量血压固定在早晚各一次，坐着休息五分钟再测，记录两周给医生看", ("监测", "血压")),
        ("med:fever", "发烧三天不退或出现意识改变要立刻就医，不要继续在家观察", ("就医", "发烧")),
        ("med:child", "儿童退烧按体重算剂量，不用成人药掰半片", ("用药", "儿童")),
        ("med:storage", "药箱每半年清一次，过期药送回收点，不冲马桶也不随手丢", ("用药", "储存")),
        ("med:record", "每次就诊后记下诊断、用药与复诊时间，换医院时直接给医生看", ("记录", "就诊")),
        ("med:fast", "抽血查血脂血糖要空腹八小时，前一天不吃油腻不喝酒", ("检查", "空腹")),
        ("med:sport", "腰椎间盘突出急性期不做弯腰负重，先做核心稳定训练", ("康复", "腰椎")),
        ("med:sleep", "长期失眠先查作息与咖啡因摄入，不要自己长期吃安眠药", ("睡眠", "失眠")),
    ],
    queries=[
        Query("我对什么药过敏", ("med:allergy",), "exact"),
        Query("抗生素要吃几天才能停", ("med:abx",), "paraphrase"),
        Query("中药和西药能一起吃吗", ("med:herb",), "exact"),
        Query("在家量血压怎么才能准", ("med:bp",), "paraphrase"),
        Query("小孩发烧吃多少药", ("med:child",), "paraphrase"),
        Query("发烧多久必须去医院", ("med:fever",), "paraphrase"),
        Query("抽血前能不能吃早饭", ("med:fast",), "paraphrase"),
        Query("过期药怎么处理", ("med:storage",), "exact"),
        Query("肾功能不好用药要注意什么", ("med:kidney",), "paraphrase"),
        Query("怎么更换汽车轮胎", (), "unanswerable"),
        Query("去年世界杯冠军是哪个队", (), "unanswerable"),
    ],
    dedupe_pairs=[
        ("抗生素必须按疗程吃完，症状好转就停容易复发", "抗生素必须按疗程吃完，症状好转就停容易复发。", True),
        ("止痛药不要空腹吃", "止痛药不要空腹吃，有胃溃疡史的人用前先问医生", False),
        ("儿童退烧按体重算剂量", "儿童退烧药不要和成人药混用", False),
        ("过期药送回收点", "过期药送回收点，不冲马桶", False),
    ],
    clusters=[
        ["med:allergy", "med:kidney", "med:interact"],
        ["med:bp", "med:fever", "med:record"],
        ["med:abx", "med:herb", "med:storage"],
    ],
)


# ---------------------------------------------------------------- 数据集：合同与合规


LEGAL = Dataset(
    name="zh-legal",
    memories=[
        ("law:ip", "对外合同必须写明知识产权归属，没写不等于归客户", ("合同", "知识产权")),
        ("law:liab", "违约责任要写赔偿上限，通常不超过合同总额，无限责任条款单独评审", ("合同", "违约")),
        ("law:term", "自动续约要提前三十天书面通知才能终止，口头通知无效", ("合同", "续约")),
        ("law:data", "收集个人信息要明示目的与范围，最小必要，不得默认勾选同意", ("合规", "个人信息")),
        ("law:cross", "跨境传输数据要先做安全评估或签标准合同，不能直接传到境外服务器", ("合规", "跨境")),
        ("law:invoice", "发票内容必须与合同一致，服务费不能开成货物，否则无法抵扣", ("财税", "发票")),
        ("law:seal", "用章走审批并登记，空白合同绝不盖章", ("合同", "用章")),
        ("law:nda", "保密期限一般两到五年，超期要重新签，离职后仍需履行", ("合同", "保密")),
        ("law:labor", "试用期最长六个月且只能约定一次，试用期工资不低于转正的八成", ("劳动", "试用期")),
        ("law:overtime", "加班要有审批记录，调休与加班费二选一，不能既不给钱也不给休", ("劳动", "加班")),
        ("law:audit", "合同原件与审批流保存至少五年，电子合同要留签署日志", ("合规", "归档")),
        ("law:vendor", "供应商准入要查营业执照与资质，付款前核对账户名与合同主体一致", ("采购", "供应商")),
        ("law:ad", "宣传文案禁用最、第一、国家级等绝对化用语，引用数据要标来源", ("合规", "广告")),
        ("law:minor", "不满十四周岁用户的数据按敏感个人信息处理，需要监护人同意", ("合规", "未成年人")),
    ],
    queries=[
        Query("合同里的知识产权归谁", ("law:ip",), "exact"),
        Query("违约金有没有上限", ("law:liab",), "paraphrase"),
        Query("自动续约怎么取消", ("law:term",), "exact"),
        Query("能不能把用户数据传到国外", ("law:cross",), "paraphrase"),
        Query("发票内容和合同不一致会怎样", ("law:invoice",), "paraphrase"),
        Query("试用期最长多久", ("law:labor",), "exact"),
        Query("加班只给调休不给钱行不行", ("law:overtime",), "paraphrase"),
        Query("宣传语能不能写行业第一", ("law:ad",), "paraphrase"),
        Query("离职后还要保密吗", ("law:nda",), "paraphrase"),
        Query("怎么写一份遗嘱", (), "unanswerable"),
        Query("机票改签要手续费吗", (), "unanswerable"),
    ],
    dedupe_pairs=[
        ("用章走审批并登记，空白合同绝不盖章", "用章走审批并登记，空白合同绝不盖章。", True),
        ("保密期限一般两到五年", "保密期限两到五年，到期要重签", False),
        ("违约金要写赔偿上限", "违约金要写赔偿上限，通常不超过合同总额", False),
        ("发票内容必须与合同一致", "发票内容必须和合同对得上，否则不能抵扣", False),
    ],
    clusters=[
        ["law:ip", "law:liab", "law:term"],
        ["law:data", "law:cross", "law:minor"],
        ["law:labor", "law:overtime", "law:audit"],
    ],
)


# ---------------------------------------------------------------- 数据集：客服工单


SUPPORT = Dataset(
    name="zh-support",
    memories=[
        ("cs:sla", "一线响应十五分钟内，超时自动升级到二线并通知值班", ("工单", "时限")),
        ("cs:tone", "先说能做什么，再解释限制，不要一上来讲流程", ("话术", "共情")),
        ("cs:refund", "七天无理由退货需商品完好，定制类与生鲜除外，退款三个工作日到账", ("售后", "退款")),
        ("cs:escalate", "同一用户两次未解决就必须升级，不要让客户重复描述问题", ("工单", "升级")),
        ("cs:record", "工单要写清现象、已尝试、客户环境三要素，禁止只写已处理", ("工单", "记录")),
        ("cs:promise", "不承诺具体到小时的时间点，只给区间，避免二次投诉", ("话术", "承诺")),
        ("cs:privacy", "核身只问订单后四位与收货人姓氏，绝不索要完整身份证号", ("安全", "核身")),
        ("cs:vip", "年消费超过五万的客户由专属客服跟进，转接不排队", ("分层", "大客户")),
        ("cs:comp", "补偿优先给权益券而不是现金，超出权限要主管审批", ("售后", "补偿")),
        ("cs:repeat", "同类问题一周出现三次以上要提单给产品，不能只安抚", ("改进", "提单")),
        ("cs:close", "关单前确认客户认可，未回复二十四小时视为默认但要留痕", ("工单", "关单")),
        ("cs:lang", "对外回复不用内部黑话与缩写，用客户能懂的说法", ("话术", "可读性")),
        ("cs:night", "夜间只有紧急故障走电话，其余次日九点前回复", ("值班", "夜间")),
    ],
    queries=[
        Query("客户催得很急怎么回", ("cs:promise", "cs:tone"), "paraphrase"),
        Query("退款多久能到账", ("cs:refund",), "exact"),
        Query("客户反复投诉同一个问题", ("cs:escalate", "cs:repeat"), "paraphrase"),
        Query("工单里要写哪些信息", ("cs:record",), "paraphrase"),
        Query("能不能问客户要身份证号", ("cs:privacy",), "exact"),
        Query("大客户怎么接待", ("cs:vip",), "paraphrase"),
        Query("能直接赔现金吗", ("cs:comp",), "exact"),
        Query("同一个问题老出现怎么办", ("cs:repeat",), "paraphrase"),
        Query("晚上客户来消息要不要回", ("cs:night",), "paraphrase"),
        Query("怎么申请专利", (), "unanswerable"),
        Query("推荐一款游戏显卡", (), "unanswerable"),
    ],
    dedupe_pairs=[
        ("关单前确认客户认可，未回复二十四小时视为默认", "关单前确认客户认可，未回复二十四小时视为默认。", True),
        ("工单要写清现象与已尝试", "工单要写清现象、已尝试、客户环境三要素", False),
        ("补偿优先给权益券", "补偿优先给权益券而不是现金", False),
        ("对外回复不用内部黑话", "对外回复要用客户能懂的说法", False),
    ],
    clusters=[
        ["cs:sla", "cs:escalate", "cs:close"],
        ["cs:tone", "cs:promise", "cs:lang"],
        ["cs:refund", "cs:comp", "cs:vip"],
    ],
)


# ---------------------------------------------------------------- 数据集：个人助理


PERSONAL = Dataset(
    name="zh-personal",
    memories=[
        ("me:coffee", "下午三点后不喝咖啡，会影响入睡，改喝无咖啡因的", ("作息", "咖啡因")),
        ("me:meeting", "上午十点前不排会，那段时间留给深度工作", ("日程", "专注")),
        ("me:travel", "出差订靠过道座位，落地先回酒店放行李再见人", ("出差", "偏好")),
        ("me:food", "不吃香菜与动物内脏，点菜时提前说", ("饮食", "忌口")),
        ("me:family", "每周日晚上给父母打电话，超过两周没打要提醒", ("家庭", "提醒")),
        ("me:budget", "每月固定转出三成收入做长期储蓄，剩下的才是可花的", ("财务", "储蓄")),
        ("me:exercise", "每周三次力量训练加两次有氧，出差用弹力带替代", ("运动", "习惯")),
        ("me:read", "读书笔记只记三件事：反直觉的点、能立刻用的、想反驳的", ("学习", "笔记")),
        ("me:gift", "送礼预算两百到五百，偏好实用而不是装饰", ("社交", "送礼")),
        ("me:inbox", "邮件每天固定两次处理，上午十一点与下午五点，其余时间关通知", ("效率", "邮件")),
        ("me:backup", "手机照片每月导出一次到硬盘，云盘只做第二份", ("数据", "备份")),
        ("me:health", "每年体检安排在生日那个月，顺便复查牙齿", ("健康", "体检")),
        ("me:sleep", "睡前一小时不看工作消息，手机放客厅充电", ("作息", "睡眠")),
        ("me:lang", "写作偏好短句与主动语态，避免进行、相关这类虚词", ("写作", "偏好")),
    ],
    queries=[
        Query("我几点之后不喝咖啡", ("me:coffee",), "exact"),
        Query("上午能不能安排会议", ("me:meeting",), "paraphrase"),
        Query("出差订什么座位", ("me:travel",), "exact"),
        Query("我有什么忌口", ("me:food",), "paraphrase"),
        Query("多久给家里打一次电话", ("me:family",), "paraphrase"),
        Query("每个月存多少钱", ("me:budget",), "paraphrase"),
        Query("邮件什么时候处理", ("me:inbox",), "paraphrase"),
        Query("送礼预算多少", ("me:gift",), "exact"),
        Query("写东西有什么风格要求", ("me:lang",), "paraphrase"),
        Query("怎么办理房产过户", (), "unanswerable"),
        Query("推荐一只指数基金", (), "unanswerable"),
    ],
    dedupe_pairs=[
        ("手机照片每月导出一次到硬盘", "手机照片每月导出一次到硬盘，云盘只做第二份", False),
        ("睡前一小时不看工作消息", "睡前一小时不看工作消息，手机放客厅充电", False),
        ("每周三次力量训练加两次有氧", "每周三次力量训练加两次有氧。", True),
        ("送礼预算两百到五百", "送礼预算两百到五百，偏好实用", False),
    ],
    clusters=[
        ["me:coffee", "me:sleep", "me:meeting"],
        ["me:food", "me:gift", "me:family"],
        ["me:budget", "me:exercise", "me:health"],
    ],
)


DATASETS: dict[str, Dataset] = {
    ds.name: ds for ds in (FINANCE, TECH, MEDICAL, LEGAL, SUPPORT, PERSONAL)
}


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
    report["summary"] = summarize(report["datasets"])
    return report


def summarize(entries: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """跨领域平均。单看一个领域容易被「这个领域好调」骗过去。

    平均的是**每领域指标的算术平均**，不是「把查询堆在一起算」——
    后者会让记忆条数多的领域主导结果（金融 39 条、法务 14 条，权重差三倍）。
    """
    if not entries:
        return {}
    def mean_of(path: tuple[str, ...]) -> float:
        values = []
        for entry in entries:
            node: Any = entry
            for key in path:
                node = (node or {}).get(key) if isinstance(node, dict) else None
            if isinstance(node, (int, float)) and not isinstance(node, bool):
                values.append(float(node))
        return round(_mean(values), 4) if values else 0.0
    return {
        "domains": len(entries),
        "recall@k": mean_of(("retrieval", "overall", "recall@k")),
        "mrr": mean_of(("retrieval", "overall", "mrr")),
        "abstain_f1": mean_of(("abstain", "abstain_f1")),
        "dedupe_f1": mean_of(("dedupe", "f1")),
        "queries": sum(int((e.get("retrieval") or {}).get("overall", {}).get("n", 0)) for e in entries),
    }


def format_report(report: dict[str, Any]) -> str:
    """人读版：每个数据集一张小表，拒绝把数字埋在 JSON 里。"""
    lines = [
        f"通道 {report['channel']} · k={report['k']} · 用时 {report['elapsed_ms']}ms",
        "",
    ]
    summary = report.get("summary") or {}
    if summary and summary.get("domains", 0) > 1:
        lines.append(
            f"跨 {summary['domains']} 领域（{summary['queries']} 条可答查询）平均："
            f" recall@k={summary['recall@k']} mrr={summary['mrr']}"
            f" 弃答F1={summary['abstain_f1']} 去重F1={summary['dedupe_f1']}"
        )
        lines.append("")
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
    # 下界不能压着「当前的默认值」：扫描区间贴边时，推荐值一定落在边界上，
    # 那是个假的最优点（第一版从 0.15 起扫，于是「推荐 0.15」其实只是边界）。
    for t in [i / 100 for i in range(4, 56)]:
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
