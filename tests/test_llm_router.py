"""LLMRouter 路由逻辑测试（不依赖网络）。

用 FakeProvider 验证三种核心路径：
1. 主厂商 402（余额不足）→ 自动切换备用厂商成功
2. 全部厂商失败 → 降级为 fallback 文本，pipeline 不崩溃
3. 无任何厂商配置密钥 → 直接降级，不发请求
"""

import pytest

from fundamental_agent.llm.base import LLMProvider, LLMRequest, LLMResponse
from fundamental_agent.llm.router import LLMRouter


class FakeProvider(LLMProvider):
    """受控厂商：mode 决定 complete 的返回。"""

    def __init__(self, name: str, mode: str = "ok", key: str = "sk-real") -> None:
        super().__init__(name=name, base_url="http://fake", model="fake", api_key=key)
        self.mode = mode
        self.calls = 0

    def complete(self, req: LLMRequest) -> LLMResponse:
        self.calls += 1
        if self.mode == "ok":
            return LLMResponse(text=f"[{self.name}] 结论", ok=True, provider=self.name)
        if self.mode == "402":
            return LLMResponse(text="", ok=False, provider=self.name, error="402 余额不足")
        return LLMResponse(text="", ok=False, provider=self.name, error="HTTP 500")


def test_primary_402_falls_back_to_secondary():
    router = LLMRouter([FakeProvider("deepseek", "402"), FakeProvider("qwen", "ok")])
    resp = router.complete(LLMRequest(system="s", user="u"))
    assert resp.ok is True
    assert resp.provider == "qwen"
    assert "qwen" in resp.text


def test_all_providers_fail_degrades_gracefully():
    router = LLMRouter([FakeProvider("deepseek", "402"), FakeProvider("qwen", "err")])
    resp = router.complete(LLMRequest(system="s", user="u"))
    assert resp.ok is False
    assert resp.text.startswith("（AI 结论生成失败")
    assert resp.error  # 带回最后一个厂商的错误，便于排查


def test_no_provider_configured_returns_fallback():
    router = LLMRouter([FakeProvider("deepseek", "ok", key="")])  # 空 key → has_key()=False
    resp = router.complete(LLMRequest(system="s", user="u"))
    assert resp.ok is False
    assert resp.error == "no_provider_configured"


def test_primary_success_no_unnecessary_call_to_secondary():
    p1 = FakeProvider("deepseek", "ok")
    p2 = FakeProvider("qwen", "ok")
    router = LLMRouter([p1, p2])
    resp = router.complete(LLMRequest(system="s", user="u"))
    assert resp.ok is True
    assert resp.provider == "deepseek"
    assert p2.calls == 0  # 主厂商成功，不浪费一次调用
