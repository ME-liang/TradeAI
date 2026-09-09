"""LLM 提供方抽象层。

设计目标：让上游（router / summarize_analysis）不感知具体厂商。
任何「OpenAI 兼容」接口（DeepSeek / 通义 / 智谱 / 本地 vLLM 等）只要
改 base_url + model + api_key 即可接入，无需新写代码。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class LLMRequest:
    """一次补全请求。system/user 为提示，temperature/max_tokens 可覆盖全局默认值。"""

    system: str
    user: str
    temperature: float = 0.3
    max_tokens: int = 2000


@dataclass
class LLMResponse:
    """一次补全结果。ok=False 时必须带 error 以便 router 决策是否切换厂商。"""

    text: str
    ok: bool
    provider: str
    error: str = ""


class LLMProvider(ABC):
    """厂商无关接口。子类只需实现 complete()。"""

    name: str = "base"

    def __init__(
        self,
        name: str,
        base_url: str,
        model: str,
        api_key: str,
        *,
        temperature: float = 0.3,
        max_tokens: int = 2000,
    ) -> None:
        self.name = name
        self.base_url = (base_url or "").rstrip("/")
        self.model = model or ""
        self.api_key = (api_key or "").strip()
        self.temperature = temperature
        self.max_tokens = max_tokens

    def has_key(self) -> bool:
        """该厂商是否已配置有效密钥。router 据此跳过未配置的厂商，避免空跑一次请求。"""
        return bool(self.api_key) and not self.api_key.startswith("sk-在这里")

    @abstractmethod
    def complete(self, req: LLMRequest) -> LLMResponse:
        """执行一次补全。返回 ok=False 表示本次不可用（router 会尝试下一个厂商）。"""
        raise NotImplementedError
