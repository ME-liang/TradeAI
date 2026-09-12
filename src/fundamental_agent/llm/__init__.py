"""LLM 门面层（多厂商路由）。

职责：
- 对外暴露 `summarize_analysis()` / `chat()` 两个稳定接口，调用方无感于底层厂商。
- 内部用 `LLMRouter` 在多个 OpenAI 兼容厂商间路由，主厂商 402/限流时自动切备用，
  全部失败则降级为「未获取」文本，绝不中断整条报告 pipeline。
- 所有提示词外置于 llm/prompts/*.md，改文案不动代码。

非密钥配置（temperature/max_tokens/providers）来自 config.yaml 的 `llm:` 段；
密钥从环境变量读取（见 .env），绝不硬编码。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import yaml
from dotenv import load_dotenv

from ..models import AnalysisResult
from .base import LLMRequest
from .router import LLMRouter
from .providers.openai_compat import OpenAICompatProvider
from .prompts.loader import load_prompt

# 项目根：src/fundamental_agent/llm/__init__.py -> parent*4 = fundamental_agent/（仓库根）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_CONFIG_PATH = _PROJECT_ROOT / "config.yaml"
_ENV_PATH = _PROJECT_ROOT / ".env"

_router: LLMRouter | None = None


def load_llm_config() -> dict:
    """读取 LLM 配置：config.yaml 的非密钥项 + 环境变量的密钥/地址。"""
    load_dotenv(_ENV_PATH)
    with open(_CONFIG_PATH, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    return dict(cfg.get("llm", {}))


def _build_router() -> LLMRouter:
    """按 config 装配厂商列表，保证向后兼容旧的「单 DeepSeek」配置。"""
    cfg = load_llm_config()
    temperature = float(cfg.get("temperature", 0.3))
    max_tokens = int(cfg.get("max_tokens", 2000))
    # 全局单轮超时上限（秒）：可被各 provider 的 request_timeout 覆盖。
    # 默认 600s 覆盖 deepseek-r1 等推理模型的长首字延迟（实测偶发 >9min）。
    global_timeout = int(cfg.get("request_timeout", 600))
    fallback = cfg.get(
        "fallback_text",
        "（AI 结论生成失败：所有模型提供商均不可用，已跳过；报告其余部分不受影响）",
    )

    providers: list[OpenAICompatProvider] = []
    plist = cfg.get("providers")
    if plist:
        for p in plist:
            key = os.getenv(p.get("api_key_env", ""), "").strip()
            providers.append(
                OpenAICompatProvider(
                    name=p.get("name", "unknown"),
                    base_url=p.get("base_url", ""),
                    model=p.get("model", ""),
                    api_key=key,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    request_timeout=int(p.get("request_timeout", global_timeout)),
                )
            )
    else:
        # 旧配置兼容：单 DeepSeek（密钥/地址从环境变量读取）
        key = os.getenv("DEEPSEEK_API_KEY", "").strip()
        base_url = os.getenv(
            "DEEPSEEK_BASE_URL", cfg.get("base_url", "https://api.deepseek.com/v1")
        )
        model = cfg.get("model", "deepseek-v4-flash")
        providers.append(
            OpenAICompatProvider(
                name=cfg.get("provider", "deepseek"),
                base_url=base_url, model=model, api_key=key,
                temperature=temperature, max_tokens=max_tokens,
                request_timeout=global_timeout,
            )
        )
    return LLMRouter(providers, fallback_text=fallback)


def get_router() -> LLMRouter:
    """懒加载单例 router，避免模块导入时即读配置/联网。"""
    global _router
    if _router is None:
        _router = _build_router()
    return _router


def chat(system: str, user: str, *, max_retries: int = 3) -> str:
    """单次补全（保留旧签名供兼容）。失败返回降级文本，不抛异常。"""
    req = LLMRequest(system=system, user=user)
    return get_router().complete(req).text


def _result_to_text(result: AnalysisResult) -> str:
    """把结构化分析结果序列化为给 LLM 阅读的纯文本。"""
    lines = [f"公司：{result.name}（{result.code}）"]
    if getattr(result, "period", None):
        lines.append(f"报告期：{result.period}")
    for m in result.modules:
        lines.append(f"\n## {m.title}")
        if m.conclusion:
            lines.append(f"模块结论：{m.conclusion}")
        for metric in m.metrics:
            if metric.applicable and metric.value is not None:
                pct = f"，行业分位 {metric.industry_percentile}%" if metric.industry_percentile is not None else ""
                lines.append(f"- {metric.label}：{metric.value}{metric.unit}{pct}")
    return "\n".join(lines)


def summarize_analysis(result: AnalysisResult) -> str:
    """基于结构化分析结果生成综合结论（经多厂商路由）。失败返回降级文本。"""
    if not result.modules:
        return "（暂无分析数据，无法生成 AI 结论）"
    data_text = _result_to_text(result)
    system = load_prompt("summary")
    user = f"请基于以下分析数据生成综合结论：\n\n{data_text}"
    return get_router().complete(LLMRequest(system=system, user=user)).text


# 各模块解读润色：LLM 返回的键 -> 与 build_data 中 PLACEHOLDER / 模板变量一一对应
_INSIGHT_KEYS = ("prof", "growth", "solv", "eff", "bench", "fin")


def _parse_insights_json(text: str) -> dict | None:
    """从 LLM 返回中解析六模块解读 JSON；容忍 markdown 代码块与多余文本。"""
    if not text:
        return None
    s = text.strip()
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", s, re.DOTALL)
    if m:
        s = m.group(1)
    else:
        start, end = s.find("{"), s.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        s = s[start : end + 1]
    try:
        obj = json.loads(s)
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    # 仅保留契约内已知键，丢弃多余字段，缺键用 None 表示「保持原解读」
    return {k: obj.get(k) for k in _INSIGHT_KEYS}


def polish_module_insights(result: AnalysisResult) -> dict | None:
    """为六个分析模块分别生成 LLM 润色后的解读文字（经多厂商路由）。失败返回 None。"""
    if not result.modules:
        return None
    data_text = _result_to_text(result)
    try:
        system = load_prompt("insights")
    except Exception:
        system = (
            "你是专业的 A 股公司研究分析师。请基于给定结构化数据，"
            "为盈利能力/成长性/偿债能力/运营效率/行业对标/财务速写六个模块分别撰写一段解读，"
            "仅返回 JSON（键：prof/growth/solv/eff/bench/fin），未知字段标'未获取'，严禁编造数字。"
        )
    user = (
        "请基于以下分析数据，为六个模块分别撰写一段精炼解读（每模块 2-4 句，不要投资建议口吻）：\n\n"
        f"{data_text}\n\n"
        "仅返回 JSON：{\"prof\":\"...\",\"growth\":\"...\",\"solv\":\"...\",\"eff\":\"...\",\"bench\":\"...\",\"fin\":\"...\"}"
    )
    try:
        resp = get_router().complete(LLMRequest(system=system, user=user)).text
    except Exception:
        return None
    return _parse_insights_json(resp)
