"""OpenAI 兼容接口的通用实现。

覆盖：DeepSeek / 通义千问（dashscope compatible-mode）/ 智谱 GLM（bigmodel v4）/
本地 vLLM 等所有 /chat/completions 形态端点。

重试与切换策略：
- 429 / 500 / 502 / 503：可重试，指数退避（最多 _MAX_TRIES 次）
- 402（余额不足）：**不重试**，直接 ok=False 让 router 切下一个厂商
- 其余 4xx（401/404 等）：不重试，直接 ok=False
- 网络瞬断（非超时）：走退避重试，耗尽后 ok=False
- 请求超时（Timeout）：单独限量重试（_MAX_TIMEOUT_RETRIES），避免单次调用在
  慢思考模型（deepseek-r1 等首字延迟常 >3min）上被反复重试、整条链路挂死 30 分钟。
"""

from __future__ import annotations

import time

import requests

from ..base import LLMProvider, LLMRequest, LLMResponse

_RETRYABLE = (429, 500, 502, 503)
_MAX_TRIES = 3
# 单轮超时上限（秒）：覆盖 deepseek-r1 等推理模型冷启动 + 长推理的首字延迟。
# 可由 config.yaml `request_timeout`（全局）或各 provider 的 `request_timeout`（单厂）覆盖。
_DEFAULT_TIMEOUT = 600
# 超时专属重试上限：超时不像 429/5xx 那样「稍等即好」，重试命中概率低，
# 故单独限制为 1 次，防止 3×600s=30min 挂死；退而让 router 切下一个厂商。
_MAX_TIMEOUT_RETRIES = 1


class OpenAICompatProvider(LLMProvider):
    def __init__(
        self,
        name: str,
        base_url: str,
        model: str,
        api_key: str,
        *,
        temperature: float = 0.3,
        max_tokens: int = 2000,
        request_timeout: int = _DEFAULT_TIMEOUT,
    ) -> None:
        super().__init__(
            name, base_url, model, api_key,
            temperature=temperature, max_tokens=max_tokens,
        )
        self.request_timeout = int(request_timeout)

    def complete(self, req: LLMRequest) -> LLMResponse:
        if not self.has_key():
            return LLMResponse(text="", ok=False, provider=self.name, error="no_api_key")

        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": req.system},
                {"role": "user", "content": req.user},
            ],
            "temperature": req.temperature,
            "max_tokens": req.max_tokens,
        }

        last_err = ""
        timeout_retries = 0
        for attempt in range(_MAX_TRIES):
            try:
                resp = requests.post(
                    url, headers=headers, json=payload, timeout=self.request_timeout
                )
                if resp.status_code == 200:
                    data = resp.json()
                    content = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
                    content = content.strip()
                    # 部分推理模型（如 distill 版）在限流/实例过载时会返回 200 但空 content。
                    # 这种「成功外壳 + 空体」必须当作失败处理，否则会被上层误判为「生成成功」，
                    # 导致整份报告静默降级为空壳。按可重试处理，让 router 退避后切下一个厂商。
                    if not content:
                        last_err = "HTTP 200 但返回内容为空（疑似限流/实例过载）"
                        time.sleep(min(2 ** attempt, 8))
                        continue
                    return LLMResponse(text=content, ok=True, provider=self.name)
                if resp.status_code == 402:
                    return LLMResponse(
                        text="", ok=False, provider=self.name,
                        error="402 余额不足，请充值后重试",
                    )
                if resp.status_code in _RETRYABLE:
                    last_err = f"HTTP {resp.status_code}（可重试）"
                    time.sleep(min(2 ** attempt, 8))
                    continue
                last_err = f"HTTP {resp.status_code} {resp.text[:160]}"
                return LLMResponse(text="", ok=False, provider=self.name, error=last_err)
            except requests.exceptions.Timeout:
                # 超时单独限量：慢思考模型在该厂商上大概率持续超时，重试收益低，
                # 限量后立即交还 router 切下一个厂商，避免链路长时间挂死。
                timeout_retries += 1
                last_err = (
                    f"请求超时（>{self.request_timeout}s，已超时重试 "
                    f"{timeout_retries}/{_MAX_TIMEOUT_RETRIES}）"
                )
                if timeout_retries > _MAX_TIMEOUT_RETRIES:
                    return LLMResponse(text="", ok=False, provider=self.name, error=last_err)
                time.sleep(min(2 ** attempt, 8))
            except requests.RequestException as exc:
                last_err = f"网络异常：{exc}"
                time.sleep(min(2 ** attempt, 8))
        return LLMResponse(text="", ok=False, provider=self.name, error=last_err)
