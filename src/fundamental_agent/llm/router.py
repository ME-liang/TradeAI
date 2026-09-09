"""多厂商路由：配置驱动的主备切换与降级。

行为约定（对应方案风险表「DeepSeek 余额耗尽」应对）：
- 按 config 中 providers 顺序尝试，主厂商优先
- 未配置密钥的厂商直接跳过（省一次必败请求）
- 某厂商返回 ok=False（402 余额 / 4xx / 重试耗尽）即切下一个厂商
- **当日熔断（OV-3）**：某厂商连续失败 ≥ _BREAKER_THRESHOLD 次后当日跳过——
  避免「余额耗尽/限流的厂商仍排首位」时每次调用都先吃一次必败往返
- 全部厂商均不可用 → 返回 ok=False + fallback_text，让上游把结论置「未获取」，
  而不是让整条 pipeline 崩溃
"""

from __future__ import annotations

import datetime
import threading

from .base import LLMProvider, LLMRequest, LLMResponse

_DEFAULT_FALLBACK = "（AI 结论生成失败：所有模型提供商均不可用，已跳过；报告其余部分不受影响）"

# OV-3（概况性能专项）：provider 当日熔断。连续失败达阈值的厂商当日不再尝试
# （跨日自动复位）。仅统计「连续失败」——成功一次即清零，避免误伤偶发限流的健康厂商。
_BREAKER_THRESHOLD = 3
_BREAKER: dict[str, list] = {}   # {provider_name: [连续失败次数, 熔断日期 iso]}
_BREAKER_LOCK = threading.Lock()


def _today() -> str:
    return datetime.date.today().isoformat()


def _breaker_skip(name: str) -> bool:
    with _BREAKER_LOCK:
        st = _BREAKER.get(name)
        return bool(st and st[1] == _today() and st[0] >= _BREAKER_THRESHOLD)


def _breaker_record(name: str, ok: bool) -> None:
    with _BREAKER_LOCK:
        today = _today()
        st = _BREAKER.setdefault(name, [0, today])
        if st[1] != today:
            st[0], st[1] = 0, today
        st[0] = 0 if ok else st[0] + 1


def reset_breaker() -> None:
    """清空熔断状态（测试用）。"""
    with _BREAKER_LOCK:
        _BREAKER.clear()


class LLMRouter:
    def __init__(
        self,
        providers: list[LLMProvider],
        *,
        fallback_text: str = _DEFAULT_FALLBACK,
    ) -> None:
        self._providers = list(providers or [])
        self._fallback = fallback_text

    @property
    def provider_names(self) -> list[str]:
        return [p.name for p in self._providers]

    def complete(self, req: LLMRequest) -> LLMResponse:
        last_err = ""
        attempted = 0
        for prov in self._providers:
            if not prov.has_key() or _breaker_skip(prov.name):
                continue
            attempted += 1
            resp = prov.complete(req)
            _breaker_record(prov.name, bool(resp.ok))
            if resp.ok:
                return resp
            last_err = resp.error  # 该厂商失败，尝试下一个
        if attempted == 0:
            return LLMResponse(
                text=self._fallback, ok=False, provider="",
                error="no_provider_configured",
            )
        return LLMResponse(text=self._fallback, ok=False, provider="", error=last_err)
