"""pragmatics.py - 语用层：一句话是**什么行为**，有多强，为什么这么说。

词法层解决「这句话里有哪些词」，语义层解决「它和哪条记忆像」，
但真正决定该不该记、记成什么的，是第三层：

    「以后都别用表格了」   —— 禁令。写错就是每次都被违反。
    「能不能别用表格」     —— 请求。可以协商，记成禁令就是过度服从。
    「表格是不是有点多」   —— 暗示。用户没直说，记成规则就是把猜测当事实。
    「我上次说过别用表格」 —— 纠正 + 不满。重点不是「别用表格」，是「你没记住」。
    「随便你」             —— **撤回沟通**。最危险的一句：它离「同意」最近，意思最远。

这就是言语行为（speech act）。同一串字，行为不同，对记忆的要求完全不同：
禁令要进 core，暗示只能进 event 并标「未确认」，撤回根本不该记成偏好。

四条设计原则：

1. **只标不猜到底。** 每个判断都带 evidence（命中了哪些词）与 confidence，
   低于阈值就退回 `statement` —— 宁可少判，不可错判成规则。
2. **强度是乘出来的，不是加的。** 基础强度 × 模糊限定（可能/好像）× 强调（一定/必须）。
   「可能别用表格」比「以后都别用表格」弱得多，这两者不该同 tier。
3. **否定与模糊分开处理。** 否定改的是命题（用/不用），模糊改的是**承诺度**（我多确定）。
   混在一起就会把「我觉得可能不要」记成「不要」。
4. **动机可以推断，但必须承认不确定。** 「又」「老是」暗示不满累积，
   「随便」可能是放弃沟通也可能是真无所谓 —— 标出来让人判断，不替人决定。
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

# ---------------------------------------------------------------- 标记词表
#
# 每条规则都是「正则 → 行为」。顺序即优先级：越具体的越先匹配。
# 用正则而不是分词，是因为语用标记大量依赖**句式**（"能不能…吗"、"是不是…了"），
# 词表本身表达不了这些。

_RULES: tuple[tuple[str, str, float], ...] = (
    # (正则, act, 基础强度)
    #
    # 顺序即优先级。这里的排序是有讲究的：
    #   · 纠正最优先 —— 「我上次说过别用表格」重点不是禁令，是「你没记住」；
    #   · 撤回第二 —— 「随便」必须抢在「偏好」之前，否则会被记成拿到许可；
    #   · 强禁令在请求之前 —— 「以后都别」比「能不能」硬；
    #   · 弱禁令在请求之后 —— 「能不能别用」是请求，不是禁令。
    (r"(说过|我说的是|不是说|你搞错|搞错了|上次就|又来了|怎么又|我说了)", "correction", 0.85),
    (r"(随便|算了|无所谓|你看着办|都行|不用了|懒得说)", "withdrawal", 0.25),
    (r"(别再|不要再|不要|以后都别|不准|不许|禁止|绝对不能|永远别|一律不)", "prohibition", 0.90),
    (r"(必须|一定要|务必|切记|永远要|每次都要|全都)", "directive", 0.85),
    (r"(给我|帮我|替我|把.{0,8}(改|换|删|加|关))", "directive", 0.70),
    (r"(能不能|能否|可否|可不可以|可以.{0,6}吗|是否方便|麻烦|要不|最好是|建议)", "request", 0.55),
    (r"别[用写说要给动做拿]", "prohibition", 0.72),
    (r"(我更喜欢|我偏好|我更想|我习惯|我希望|我想要|倾向于)", "preference", 0.60),
    (r"(是不是|有点|好像|感觉|总是|老是|太多了|不太对|为什么还|怎么还)", "complaint", 0.35),
    (r"^(对|没错|可以|行|好|ok|嗯)[，,。！!]?", "confirmation", 0.70),
    (r"(我是|我做|我的习惯|我一般|我从不|我向来)", "self_disclosure", 0.65),
    (r"(吗|呢|？|\?)$", "question", 0.30),
)

# 模糊限定：降低**承诺度**，不影响命题
_HEDGE = re.compile(r"(可能|也许|大概|好像|似乎|估计|或许|不一定|看情况|应该吧)")
# 强调：提高强度
_EMPHASIS = re.compile(r"(一定|必须|务必|千万|绝对|永远|每次|全都|不要任何)")
# 不满累积
_FRUSTRATION = re.compile(r"(又|老是|总是|还是|反复|说了几遍|几次了)")
# 礼貌缓冲（避免冲突的动机）
_POLITE = re.compile(r"(能不能|麻烦|请|好不好|行不行|要不)")
# 软化（缓和语气）
_SOFTEN = re.compile(r"(其实|只是|不过|稍微|有点|可能)")

# 场景/语域：同一句话在不同场合分量不同
_REGISTER_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("review", ("复盘", "总结", "回顾", "这周", "今天亏", "做错")),
    ("execution", ("买入", "卖出", "下单", "止损", "加仓", "减仓", "盘口")),
    ("coding", ("代码", "测试", "部署", "提交", "接口", "报错", "函数")),
    ("chat", ()),  # 兜底
)

# 行为 → 建议的存储层级。注意：这是**建议**，调用方可以覆盖。
_ACT_TIER: dict[str, str] = {
    "prohibition": "core",
    "directive": "fact",
    "preference": "fact",
    "self_disclosure": "fact",
    "correction": "event",
    "complaint": "event",
    "request": "event",
    "hint": "event",
    "withdrawal": "event",
    "confirmation": "event",
    "question": "ephemeral",
    "statement": "event",
}

# 这些行为**不该**被当成「用户表达了一个稳定偏好」
# 这些行为**不该**被当成「用户表达了一个稳定偏好」。
# 纠正也在里面：「我上次说过别用表格」的主要行为是"你没记住"，
# 把它记成"用户偏好表格"会把不满消解成一条中性偏好 —— 最糟的一种误读。
_NOT_A_PREFERENCE = frozenset({"withdrawal", "question", "hint", "complaint", "correction"})


@dataclass
class Speech:
    """一句话的语用档案。它是记忆的元数据，不是内容本身。"""

    act: str = "statement"
    force: float = 0.5  # [0,1] 这句话有多「硬」
    register: str = "chat"  # 场景：review / execution / coding / chat
    motive: str = ""  # 推断的动机；空 = 没看出来（诚实）
    hedged: bool = False  # 有模糊限定 → 不能当硬规则
    confidence: float = 0.4  # 对上面这套判断有多信
    evidence: tuple[str, ...] = ()  # 命中的语言标记，供人复核

    def validate(self, name: str = "speech") -> None:
        if not isinstance(self.act, str) or not self.act:
            raise ValueError(f"{name}.act 非法: {self.act!r}")
        if not 0.0 <= float(self.force) <= 1.0:
            raise ValueError(f"{name}.force 必须落在 [0,1]: {self.force!r}")
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise ValueError(f"{name}.confidence 必须落在 [0,1]: {self.confidence!r}")
        if not isinstance(self.evidence, tuple):
            raise ValueError(f"{name}.evidence 必须是 tuple")

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["evidence"] = list(self.evidence)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "Speech":
        if not d:
            return cls()
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        if "evidence" in known:
            known["evidence"] = tuple(known["evidence"] or ())
        speech = cls(**known)
        return speech

    # ---- 供存储层使用 ----

    def suggested_tier(self) -> str:
        """建议的存储层级。

        不是按行为查表就完：「我觉得可能不要用表格」的行为是禁令，但它带模糊限定、
        强度只有 0.54 —— 进 core 就成了「每次都必须遵守」，而用户其实只是随口一说。

        所以两级衰减：
          · 禁令/要求 强度 < 0.8 → 只算 fact（可协商），不进 core；
          · 带模糊限定的 fact → 降到 event（记下来，但别当规矩）。
        """
        tier = _ACT_TIER.get(self.act, "event")
        if tier == "core" and self.force < 0.8:
            tier = "fact"
        if self.hedged and tier == "fact":
            tier = "event"
        return tier

    def is_preference(self) -> bool:
        """它是不是「用户表达了一个稳定偏好」。

        撤回、提问、暗示、抱怨都不算 —— 把「随便你」记成偏好是这类系统最典型的
        自欺：用户放弃了沟通，系统却当成拿到了许可。
        """
        return self.act not in _NOT_A_PREFERENCE and self.confidence >= 0.5

    def label(self) -> str:
        """给注入块看的短标签。人话，不是枚举值。"""
        return _ACT_LABEL.get(self.act, self.act)


_ACT_LABEL = {
    "prohibition": "禁令",
    "directive": "要求",
    "preference": "偏好",
    "request": "请求",
    "correction": "纠正",
    "complaint": "不满",
    "withdrawal": "撤回",
    "confirmation": "确认",
    "self_disclosure": "自述",
    "question": "提问",
    "hint": "暗示",
    "statement": "陈述",
}


def detect_register(text: str, default: str = "chat") -> str:
    """从内容猜场景。猜不出就返回 default，不要硬猜。

    场景影响的是「同一句话有多重」：复盘里说「不该追高」是教训，
    闲聊里说是感慨 —— 前者该进 fact，后者进 event 就够。
    """
    low = (text or "").lower()
    for name, hints in _REGISTER_HINTS:
        if hints and any(h in low for h in hints):
            return name
    return default


def classify(text: str, register: str | None = None) -> Speech:
    """把一句话归成言语行为 + 强度 + 动机。规则法，零依赖，可解释。

    为什么不用模型：这一步会在**每次写入**时跑，且它的输出直接决定 tier 与 pin。
    把一个不可审计的模型放在这个位置，等于把记忆的准入门槛交给一个黑箱。
    规则能覆盖的只是显式标记，但每一条都能指着原话说「因为这里写了『以后都别』」。
    """
    source = (text or "").strip()
    low = source.lower()
    if not low:
        return Speech()

    act, base = "statement", 0.5
    evidence: list[str] = []
    for pattern, name, strength in _RULES:
        found = re.search(pattern, low)
        if found:
            act, base = name, strength
            evidence.append(found.group(0))
            break

    hedged = bool(_HEDGE.search(low))
    if hedged:
        evidence.append(_HEDGE.search(low).group(0))
    emphasis = bool(_EMPHASIS.search(low))
    if emphasis:
        evidence.append(_EMPHASIS.search(low).group(0))

    force = base
    if hedged:
        force *= 0.6  # 模糊限定直接打折：说「可能」就不是在要求
    if emphasis:
        force = min(1.0, force * 1.15 + 0.05)
    # 程度词：「有点」这类抱怨通常不重，但也别轻到无意义
    force = max(0.05, min(1.0, force))

    motive = ""
    if _FRUSTRATION.search(low):
        motive = "不满累积"
        evidence.append(_FRUSTRATION.search(low).group(0))
    elif act == "correction":
        motive = "觉得没被记住"
    elif act == "withdrawal":
        motive = "放弃沟通（不等于同意）"
    elif _POLITE.search(low):
        motive = "避免冲突"
        evidence.append(_POLITE.search(low).group(0))
    elif _SOFTEN.search(low):
        motive = "缓和语气"

    # 置信度：命中标记越多越有把握；纯陈述句承认自己在猜
    confidence = 0.35 + 0.2 * min(3, len(evidence))
    if act == "statement":
        confidence = 0.3
    confidence = round(min(0.95, confidence), 3)

    return Speech(
        act=act,
        force=round(force, 3),
        register=register or detect_register(source),
        motive=motive,
        hedged=hedged,
        confidence=confidence,
        evidence=tuple(dict.fromkeys(evidence)),
    )


def explain(speech: Speech) -> str:
    """一行可读解释，给 CLI / 面板用。"""
    parts = [f"{speech.label()}（强度 {speech.force:.2f}，置信 {speech.confidence:.2f}）"]
    if speech.register != "chat":
        parts.append(f"场景 {speech.register}")
    if speech.motive:
        parts.append(f"动机 {speech.motive}")
    if speech.hedged:
        parts.append("含模糊限定")
    if speech.evidence:
        parts.append("依据 " + "/".join(speech.evidence))
    return "｜".join(parts)
