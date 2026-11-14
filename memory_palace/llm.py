"""llm.py - 把「规则监控」升级成「LLM 反思」：模型提假设，代码管边界。

规则引擎能回答「哪个指标越界了」，回答不了「这一周到底发生了什么」。
前者是阈值告警，后者才是复盘。区别在于：复盘要读事件序列、找因果、提假设，
而阈值告警只需要比较两个数。

所以分工是这样划的，**不是**「让模型接管」：

    规则引擎   算数字。指标、分位数、越界与否 —— 这些必须可复算、可对账。
    LLM       读证据（指标 + 事件 + 记忆样本 + 自述），提出假设与措辞。
    代码       校验结构、白名单、值域、上限；模型说的话一律先当「待验证提案」。

三条硬约束（任何一条破了，这个功能就该关掉）：

1. **模型不改配置。** 它只能输出 `suggested_params`，真正的改动走 `SelfCorrector`
   的白名单 + 值域 + 护栏 + 可回滚。让模型直接写配置，等于让它改自己的人格。
2. **输出必须结构化。** 解析失败就退回规则引擎，并记 `degraded`。
   一段无法解析的自然语言不是「反思」，只是噪声。
3. **输入必须有界。** 证据按字符预算截断：无限喂记忆进去，成本和延迟都会失控，
   而且会诱导模型对着长文本编故事。

诚实的一句：接上 LLM 之后，「发现问题的能力」来自模型，「判断改不改进」仍然来自
可复算的指标。前者不可审计，后者可审计 —— 所以只有后者能改东西。
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, runtime_checkable

from .selfcorrect import ALLOWED_PARAMS


class LLMError(RuntimeError):
    pass


@runtime_checkable
class LLMProvider(Protocol):
    name: str

    def complete(self, system: str, prompt: str, max_tokens: int = 800) -> str: ...


class CallableProvider:
    """把一个 `(system, prompt) -> str` 的函数包成 provider（测试与本地模型用）。"""

    def __init__(self, fn: Callable[[str, str], str], name: str = "callable") -> None:
        self._fn = fn
        self.name = name
        self.calls = 0

    def complete(self, system: str, prompt: str, max_tokens: int = 800) -> str:
        self.calls += 1
        return self._fn(system, prompt)


class HttpChatProvider:
    """OpenAI 兼容的 `/v1/chat/completions`（零依赖，urllib）。

    兼容面故意做窄：只用最通用的字段。本地 ollama / vLLM / LM Studio、
    以及任何云厂商的 OpenAI 兼容端点都能直接用。
    """

    def __init__(
        self,
        endpoint: str,
        model: str,
        api_key: str = "",
        timeout: float = 30.0,
        temperature: float = 0.2,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.temperature = temperature
        self.name = f"http:{model}"

    def complete(self, system: str, prompt: str, max_tokens: int = 800) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "temperature": self.temperature,
            "max_tokens": max_tokens,
        }
        req = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}),
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise LLMError(f"反思请求失败: {exc}") from None
        try:
            return str(body["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError):
            raise LLMError(f"响应格式不认识: {str(body)[:200]}") from None


SYSTEM_PROMPT = """你是一个长期记忆系统的复盘者。你只读证据，不执行任何操作。

硬性要求：
1. 只输出一个 JSON 对象，不要 markdown 代码块，不要解释文字。
2. 字段固定为：findings（字符串数组，最多 5 条，指出具体现象）、
   hypotheses（字符串数组，最多 3 条，可验证的假设）、
   suggested_params（数组，最多 2 条，每条形如 {"param": 参数名, "direction": "up"|"down", "why": 简短理由}）、
   confidence（0 到 1 的小数，表示你对本轮结论的整体信心）。
3. 不要编造数字。你看到的数字是规则引擎算出来的；你没有看到的数据就说"证据不足"。
4. suggested_params 只能从给定清单里选，清单外的一律不要提。
"""


@dataclass
class LLMReflection:
    findings: list[str] = field(default_factory=list)
    hypotheses: list[str] = field(default_factory=list)
    suggested_params: list[dict[str, str]] = field(default_factory=list)
    confidence: float = 0.0
    provider: str = ""
    elapsed_ms: float = 0.0
    degraded: bool = False
    error: str = ""
    prompt_chars: int = 0
    rejected_params: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "findings": self.findings,
            "hypotheses": self.hypotheses,
            "suggested_params": self.suggested_params,
            "confidence": round(self.confidence, 3),
            "provider": self.provider,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "degraded": self.degraded,
            "error": self.error,
            "prompt_chars": self.prompt_chars,
            "rejected_params": self.rejected_params,
        }


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


class LLMReflector:
    """证据 → 结构化反思。可调用对象形式直接挂到 ReflectionEngine 上。"""

    def __init__(
        self,
        provider: LLMProvider,
        *,
        max_evidence_nodes: int = 10,
        max_events: int = 24,
        max_chars: int = 6000,
        node_chars: int = 120,
        max_tokens: int = 700,
    ) -> None:
        self.provider = provider
        self.max_evidence_nodes = max(1, int(max_evidence_nodes))
        self.max_events = max(0, int(max_events))
        self.max_chars = max(800, int(max_chars))
        self.node_chars = max(40, int(node_chars))
        self.max_tokens = max(128, int(max_tokens))
        self.last: LLMReflection | None = None

    # ---- 证据 ----

    def build_prompt(self, payload: dict[str, Any]) -> str:
        """证据按预算拼装。**参数清单与指令永远不被截断**。

        这条顺序是有代价才知道的：先前版本直接在总文本上截断，
        结果「可选参数清单」被砍掉一半 —— 模型照着半份清单提建议，
        一半合法、一半不合法，看起来像是模型在乱说，其实是提示词被截了。
        """
        head: list[str] = []
        metrics = payload.get("metrics") or {}
        head.append("【指标（规则引擎计算，未经模型修改）】")
        for key in sorted(metrics)[:24]:
            value = metrics[key]
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                head.append(f"- {key} = {value:.4g}")
        narrative = payload.get("narrative") or {}
        if narrative:
            head.append("【自述健康度】")
            for key, value in narrative.items():
                if value is None:
                    continue
                head.append(f"- {key} = {value}")

        events: list[str] = []
        raw_events = payload.get("recent_events") or []
        if raw_events:
            events.append("【最近事件】")
            for event in raw_events[: self.max_events]:
                kind = event.get("kind", "?")
                body = event.get("payload") or {}
                detail = ", ".join(f"{k}={_clip(v, 40)}" for k, v in list(body.items())[:4])
                events.append(f"- t={event.get('ts', 0):.0f} {kind} {detail}".rstrip())

        nodes: list[str] = []
        raw_nodes = payload.get("sample_nodes") or []
        if raw_nodes:
            nodes.append("【记忆样本】")
            for node in raw_nodes[: self.max_evidence_nodes]:
                nodes.append(
                    f"- [{node.get('tier', '?')}/{node.get('scope', '?')}] "
                    f"{_clip(node.get('content', ''), self.node_chars)}"
                )

        tail = [
            "",
            "可选参数清单（suggested_params 只能从这里选）：",
            ", ".join(sorted(ALLOWED_PARAMS)),
            "",
            "请按 system 里的 JSON 结构输出。",
        ]
        tail_text = "\n".join(tail)
        budget = max(200, self.max_chars - len(tail_text))
        # 先丢样本，再丢事件；指标是复盘的地基，永远保留
        while nodes and len("\n".join(head + events + nodes)) > budget:
            nodes.pop()
        body = "\n".join(head + events + nodes)
        if len(body) > budget:
            body = body[: max(0, budget - 24)] + "\n…（证据已按预算截断）"
        return f"{body}{tail_text}"

    # ---- 解析 ----

    def parse(self, raw: str) -> LLMReflection:
        result = LLMReflection(provider=getattr(self.provider, "name", ""))
        data: Any = None
        try:
            data = json.loads(raw)
        except ValueError:
            match = re.search(r"\{.*\}", raw or "", re.S)
            if match:
                try:
                    data = json.loads(match.group(0))
                except ValueError:
                    data = None
        if not isinstance(data, dict):
            result.degraded = True
            result.error = "输出不是可解析的 JSON 对象"
            return result
        result.findings = [str(x).strip() for x in (data.get("findings") or []) if str(x).strip()][:5]
        result.hypotheses = [
            str(x).strip() for x in (data.get("hypotheses") or []) if str(x).strip()
        ][:3]
        try:
            result.confidence = max(0.0, min(1.0, float(data.get("confidence") or 0.0)))
        except (TypeError, ValueError):
            result.confidence = 0.0
        for item in (data.get("suggested_params") or [])[:4]:
            if not isinstance(item, dict):
                continue
            param = str(item.get("param") or "")
            direction = str(item.get("direction") or "")
            if param not in ALLOWED_PARAMS:
                # 白名单外的一律丢弃并记录：模型无权提出改白名单之外的参数
                result.rejected_params.append(param or "<empty>")
                continue
            if direction not in ("up", "down"):
                result.rejected_params.append(f"{param}:{direction}")
                continue
            result.suggested_params.append(
                {"param": param, "direction": direction, "why": _clip(item.get("why", ""), 120)}
            )
        result.suggested_params = result.suggested_params[:2]
        if not (result.findings or result.hypotheses):
            result.degraded = True
            result.error = "JSON 合法但没有 findings/hypotheses"
        return result

    # ---- 入口 ----

    def reflect(self, payload: dict[str, Any]) -> LLMReflection:
        prompt = self.build_prompt(payload)
        started = time.perf_counter()
        try:
            raw = self.provider.complete(SYSTEM_PROMPT, prompt, max_tokens=self.max_tokens)
        except LLMError as exc:
            result = LLMReflection(
                provider=getattr(self.provider, "name", ""),
                degraded=True,
                error=str(exc),
                prompt_chars=len(prompt),
                elapsed_ms=(time.perf_counter() - started) * 1000,
            )
            self.last = result
            return result
        result = self.parse(raw)
        result.prompt_chars = len(prompt)
        result.elapsed_ms = (time.perf_counter() - started) * 1000
        self.last = result
        return result

    def as_callable(self) -> Callable[[dict[str, Any]], list[str]]:
        """给 `ReflectionEngine(reflector=...)` 用的适配器：只回文字，不进配置。"""

        def hook(payload: dict[str, Any]) -> list[str]:
            result = self.reflect(payload)
            if result.degraded:
                # 降级要留痕：不能让人以为「这轮模型看过」
                return [f"LLM 反思降级：{result.error}"]
            lines = list(result.findings)
            for hyp in result.hypotheses:
                lines.append(f"假设：{hyp}")
            for item in result.suggested_params:
                lines.append(
                    f"建议参数 {item['param']} 向 {item['direction']}（{item['why']}）"
                    " —— 未生效，需经护栏验证"
                )
            return lines

        return hook


def make_reflector(config: Any) -> LLMReflector | None:
    """按配置造反思器；没配就返回 None（规则反思照常工作）。"""
    provider = getattr(config, "llm_provider", "") or ""
    if not provider:
        return None
    if provider == "http":
        key_env = getattr(config, "llm_key_env", "") or ""
        provider_obj: LLMProvider = HttpChatProvider(
            getattr(config, "llm_endpoint", ""),
            getattr(config, "llm_model", "") or "default",
            os.environ.get(key_env, "") if key_env else "",
            timeout=float(getattr(config, "llm_timeout", 30.0)),
            temperature=float(getattr(config, "llm_temperature", 0.2)),
        )
        return LLMReflector(
            provider_obj,
            max_evidence_nodes=int(getattr(config, "llm_evidence_nodes", 10)),
            max_chars=int(getattr(config, "llm_max_chars", 6000)),
        )
    return None
